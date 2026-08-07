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
| `status` | versions, available updates, pending restarts |
| `doctor` | missing dependencies, duplicate jars, wrong Java, files changed by hand |

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
  still `!!MCDR plugin reload` until the W4VE runtime exists.

## Configuration

| Variable | Meaning |
|---|---|
| `W4VE_INDEX` | catalog path or URL (default: the published `index.json`) |
| `W4VE_SERVER` | server folder, instead of looking upwards from the current one |
| `NO_COLOR` | plain output |

MIT. Part of [W4VE](https://github.com/CodeW4VE), Ware 4 Vanilla Experience.
