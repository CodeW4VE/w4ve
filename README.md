# w4ve

The command line for the [W4VE](https://w4ve.xyz) catalog: install and update the
mods and plugins a technical Minecraft server runs, without remembering which jar
went where.

```
$ w4ve install --profile technical-survival
ok  fabric-api  0.102.0+1.21  (waits for restart)
ok  carpet  1.4.147  (waits for restart)
ok  shapeboard  1.6.0  (waits for restart)
ok  primebackup  1.13.1-w4ve.1  (reload it in place)
...

7 pieces waiting for a restart: fabric-api, carpet, carpet-extra, servux, ...
run `docker restart -t 60 smp` now? [y/N]
```

Standard library only, one file, Python 3.8 or newer. Copy it to a server and it
works. No pip, no virtualenv, nothing to install first.

## Install

```sh
curl -O https://raw.githubusercontent.com/CodeW4VE/w4ve/main/w4ve.py
chmod +x w4ve.py && sudo mv w4ve.py /usr/local/bin/w4ve
```

## Use

```sh
cd /path/to/your/server
w4ve init                       # writes w4ve.toml
w4ve adopt                      # writes down the mods this server already has
w4ve install shapeboard         # downloads it, puts it in mods/, writes it down
w4ve status                     # what is installed, what needs a restart
w4ve doctor                     # what is wrong before the server finds out
```

| Command | What it does |
|---|---|
| `init` | write a `w4ve.toml` for this folder (`--client` for a game instance) |
| `adopt` | take over a server that already exists: read what is in `mods/` and `plugins/` and write it down |
| `list` | what the catalog has (`--type`, `--side`, `--profile`, `--installed`) |
| `search <text>` | find a piece |
| `info <id>` | versions, dependencies, where it installs to |
| `profiles` | curated sets: a whole technical server in one command |
| `install <id>` | download and place it. `id@version` pins a version |
| `update [id]` | move to the newest version and write it down |
| `remove <id>` | delete the file and forget it |
| `sync` | leave the server exactly as `w4ve.toml` says |
| `status` | what is running, versions, available updates, pending restarts |
| `doctor` | missing dependencies, duplicate jars, wrong Java, files changed by hand |
| `repair` | undo an install that was killed halfway |
| `plugins` | what MCDR plugins this server has, and what is wrong with them |

## Packages

A `.w4ve` is a zip with a `w4ve.json` in it: one file that carries a piece
whole, jars and all. It exists for the pieces a jar cannot cover, the ones
whose second half is a process rather than a file.

```sh
w4ve pack ./mypiece                 # build mypiece-1.0.0.w4ve out of a folder
w4ve install ./mypiece-1.0.0.w4ve   # put every part where it goes
w4ve remove mypiece                 # take out exactly what it put in
```

- **One package, every Minecraft version.** ShapeBoard ships thirteen jars in
  one release; the package carries all of them and the install picks the one
  for this server. Two that match is the package being wrong, and it is said at
  install time rather than found by Fabric at boot.
- **A piece can bring a service.** A `service` component declares a process:
  command, working folder, restart policy, the Python modules it needs and how
  to ask whether it is up. It lands as a `[services.<name>]` block in your
  `w4ve.toml`, with a comment saying which piece put it there, and the guardian
  watches it like it watches the server. A service that needs another one first
  says `after = "the-other"` and is held back until it is really up, not merely
  started. `health_url` asks over HTTP; `health_tcp` is for a daemon that
  speaks its own protocol and could never answer a URL. What a service is asked
  with is a signal, not a console command, and its grace is ten seconds rather
  than the ninety a world needs to save.
- **A piece can ask for a restart.** A `watch` component says which console
  line to look for and what to do about it. RegionCast stops the server to copy
  region files and prints one line asking to come back; under MCDR that costs a
  plugin whose entire job is that line, and here it is three fields in the
  manifest. The restart is a request from the piece doing the work, it goes in
  the journal, and it does not count as a crash.
- **Declared, not conjured.** Python dependencies are checked by `w4ve doctor`,
  against the interpreter the service will really use, so a service that would
  die on startup is known before it starts. Installing them is your call.
- **Nothing half installed, even if you kill it.** Every step is written to a
  journal on disk before it happens, so a package interrupted by a crash or a
  `SIGKILL` is found by the next command and undone: files removed, replaced
  ones put back, services and watches taken out of `w4ve.toml`. `w4ve repair`
  does it on purpose and `w4ve doctor` reports it.
- **A config you edited is yours.** Components marked `keep_on_update` are
  never overwritten and never deleted by `remove`: the new default arrives as a
  `.new` file next to yours.

## MCDR plugins

W4VE is meant to replace MCDReforged without you losing the plugins you
already run, so the first thing it learned is how to read them.

```sh
w4ve plugins                    # every plugin, its version, and what is wrong
w4ve plugins --load             # actually load them, to see which ones run
```

It reads the four shapes a plugin comes in (a `.py` file, a folder with a
manifest, a `.mcdr`, a `.pyz`) **without running any of them**, which is the
point: the answer to "why is this plugin not loading" should not require
loading it. It works out the order dependencies force, and it says the things
that only show up in a real server folder: two copies of the same plugin, a
dependency nobody installed, a backup that is not a plugin, a file that does
not parse.

`--load` is the rehearsal for a cutover: the plugins are loaded for real,
against nothing, so you find out which ones run under W4VE before anything
moves.

**Thirteen out of thirteen of the plugins MineWave runs load, and they work.**
Against a real 1.21 world, with no MCDR installed anywhere: QuickBackupM makes
a real backup, TimedQBM hears about it through the plugin-to-plugin event,
`!!help` mixes everyone's help in words rather than translation keys, and a
plugin reloads in place and picks up a config you edited by hand.
[docs/ensayo-de-cutover.md](docs/ensayo-de-cutover.md) is the report, including
every hole a real plugin fell into on the way.

Two kinds of failure are said with different words, because they are different
problems. A plugin reaching for a name this host does not carry is **our** hole,
and it is told which name. A plugin missing a pip package of its own would be
missing it under MCDR too, and `w4ve plugins` prints the exact `pip install`
line for it. It never installs anything itself.

### A process that is not a plugin

ChatBridge is four processes: the plugin, a daemon, a client that answers
`!!online` over RCON, and a Discord bot. **The daemon imports `mcdreforged`
too**, and it is not a plugin: it is a separate process started from a command
line. Under MCDR the answer is to pip install all of MCDR on that machine to
get one class out of it.

W4VE ships the package instead. `compat/mcdreforged/` re-exports what the host
already has, nothing reimplemented, so the daemon and the plugin hold the same
`Serializable` and a fix in one is a fix in both. A service declares `mcdr =
true` and the guardian puts it on that process's `PYTHONPATH`.

Everything here is reimplemented from documented behaviour, never copied. MCDR
is LGPL-3.0 and copying its code would pull that licence onto W4VE.
`docs/mcdr-compat-surface.md` counts what running these plugins will cost.

## Running the server

Since 0.2.0 w4ve does not only install the files, it can own the server process
itself. This is [RFC-0001](docs/rfc/RFC-0001-guardian-y-propiedad-del-proceso.md)
and it needs `runtime.py` next to `w4ve.py`.

```sh
w4ve run                        # start the server and watch it, in the foreground
w4ve status                     # starting / running / stopped, and whether it is ready
w4ve console                    # attach to the live console, detach without harm
w4ve stop                       # tell it to save and wait, escalate only if it refuses
w4ve logs -f                    # the console, or --journal for what the guardian did
```

Three things it does on purpose:

- **A server it did not start is still reachable.** With no stdin of ours the
  fallback is RCON, read from the server's own `server.properties` when it is
  needed and never copied into W4VE's state or logs. Stopping an adopted server
  asks it over RCON first and only escalates to signals if nothing answers.
- **Killing the guardian never kills the server.** Come back and it finds the
  process again by `(pid, starttime)`, reports it as `adopted, no console`, and
  does not pretend to be in control it does not have.
- **Readiness is not liveness.** A server generating a world is alive and not
  ready, and it says so, which is the difference between a slow boot and a
  hang.
- **It knows what it cannot do.** On Paper it owns the process, reads the
  console and stops it cleanly, and it tells you plainly that Bukkit plugins do
  not come from the W4VE catalog.

Services declared in `w4ve.toml` are watched the same way, which is how the
second half of a piece (an image service, a scanner, a bridge) stops being a
cron nobody looks at. ChatBridge's daemon and its `!!online` client are two
blocks in the file rather than two systemd units written by hand, and one
`w4ve run` brings up the server, both of them and the plugins that talk to
them.

Every command takes `-n` to say what it would do and change nothing, and `-y` to
answer yes to everything (for scripts and cron).

## The two files

```
w4ve.toml         you edit this. It says what the server should have.
w4ve/state.json   generated. It says what the server actually has.
```

`w4ve sync` makes the second match the first. That is the whole model.

```toml
[server]
kind     = "server"          # or "client", for a game instance
mc       = "1.21"
java     = "21"
game_dir = "server"          # where Minecraft runs. "server" under MCDR, "." on its own
restart  = "docker restart -t 60 smp"

[pieces]
fabric-api = "0.102.0+1.21"
shapeboard = "1.6.0"
primebackup = "*"           # whatever is newest
```

Commands that install something write the version back into this file, so it
never goes stale. Comments and layout are kept: it is a file for a person to
read, and it is the one place where the state of a server is written down.

## Rules it follows

- **It never restarts your server.** It tells you what is pending and offers to
  run the restart command from your `w4ve.toml`. Restarting a server with people
  on it without asking is not a feature.
- **It never deletes what it did not install.** `sync` reports files it does not
  manage. Guessing that a file is garbage is how you lose a world.
- **Jars go to `mods/`**, where Fabric expects them, not into a private folder.
  Stop using w4ve and the server still boots.
- **Downloads land as a temporary file and move into place when complete.** A
  half downloaded jar in `mods/` is a server that does not start.
- **Hashes are checked when the source publishes one** (Modrinth does, GitHub
  does not) and pinned on first download, so `doctor` can tell you that a file
  changed behind your back.
- **It reads the manifest inside a jar, not its file name.** File names lie:
  `Carpet Extra.jar`, `ferritecore.jar` and `ServerReplay-1.1.2+mc1.21 (1).jar`
  are all real files on a real server.
- **A conflict needs versions on both sides.** Carpet TIS 1.82 does not boot
  with Lithium 0.13, but 1.64 with 0.13 is fine. Telling a healthy server it is
  broken is how a tool teaches people to ignore it.

## What it does not do yet

- Install `.w4ve` packages. The format is
  [designed](https://github.com/CodeW4VE/registry) but nothing publishes one yet,
  so today the CLI installs jars and `.mcdr` files straight from the catalog.
- Talk to more than one server at a time. One `w4ve.toml`, one server.
- Reload a plugin in place. It tells you which ones can be reloaded; doing it is
  still `!!MCDR plugin reload` until the plugin platform exists.
- Run MCDR plugins itself. The runtime owns the process; the compatibility host
  for `mcdreforged.api.all` is a later milestone.
- Update the guardian without restarting the server. The server survives the
  guardian today, but it gets its console back on the next start W4VE does.

## Configuration

| Variable | Meaning |
|---|---|
| `W4VE_INDEX` | catalog path or URL (default: the published `index.json`) |
| `W4VE_SERVER` | server folder, instead of looking upwards from the current one |
| `NO_COLOR` | plain output |

MIT. Part of [W4VE](https://github.com/CodeW4VE), Ware 4 Vanilla Experience.
