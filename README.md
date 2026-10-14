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

Standard library only. The complete runtime includes the catalog command,
guardian, package manager, API, compatibility layer and plugin SDK. Installing
only `w4ve.py` does not provide that runtime.

## Install

The complete distribution is currently built from this checkout; a public
runtime release is still pending. On Linux, with Python available:

```sh
python3 tools/build_distribution.py --out build/distribution
python3 install.py --archive build/distribution/w4ve-0.3.0.tar.gz
~/.local/bin/w4ve --help
```

The archive contains all runtime modules and a SHA256 manifest. The installer
validates it before activation, keeps versioned releases under
`~/.local/share/w4ve/releases/` and atomically switches `~/.local/bin/w4ve`.
Use `--prefix` and `--bin-dir` to choose other locations. It does not restart
servers or install piece dependencies. Add the bin directory to your PATH.

To reactivate a previously installed version:

```sh
python3 install.py --rollback 0.3.0
```

Rollback rechecks the installed files. It switches the runtime command; it does
not roll back worlds, databases, piece configuration or a running guardian.
The manifest verifies content integrity; verify the archive's provenance before
installing it. Runtime operation, update recovery and installed-piece tests
remain release gates; a successful `--help` is not that certification.

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
| `configure <id>` | answer what a piece needs told to it, discovering ids rather than asking for them |
| `http [nginx\|caddy]` | the web server configuration the installed pieces need |
| `storage` | what the pieces are keeping on this disk, and `--purge` to make room |

## Packages

A `.w4ve` is a zip with a `w4ve.json` in it: one file that carries a piece
whole, jars and all. It exists for the pieces a jar cannot cover, the ones
whose second half is a process rather than a file.

```sh
w4ve install wavechat-server        # from the catalog, like anything else
w4ve pack ./mypiece                 # build mypiece-1.0.0.w4ve out of a folder
w4ve install ./mypiece-1.0.0.w4ve   # or install one you have on disk
w4ve remove mypiece                 # take out exactly what it put in
```

- **The catalog can hand you one.** A piece whose release is a `.w4ve` is
  installed by name, the same as a jar: it is downloaded, unpacked into its
  places, and the archive is not left lying around. Without that the catalog
  could only ever offer the half of a piece that happens to be a file.
- **One package, every Minecraft version.** ShapeBoard ships thirteen jars in
  one release; the package carries all of them and the install picks the one
  for this server. Two that match is the package being wrong, and it is said at
  install time rather than found by Fabric at boot.
- **A piece can ask for what it needs to know.** A `settings` block turns a
  piece from something only its author can install into something anybody can:
  a public domain, a token, a channel. Ordinary answers land in your
  `w4ve.toml` with the piece's own help text as the comment above each line;
  secrets land in `w4ve/secrets.json`, mode 600, and nowhere else. `w4ve
  configure <piece>` asks, and **an id is never asked for, it is discovered**:
  with a Discord token in hand it lists the servers, channels and roles by
  name and you pick a number. What is answered arrives in the service's
  environment as `W4VE_PUBLIC_URL` and friends, so most pieces need no config
  template at all. A service with an unanswered `${secret:...}` in it is not
  started, because starting it with a hole where the token goes fails later and
  unreadably.
- **A piece can bring a plugin.** A `plugin` component carries a native plugin
  (RFC-0003) and lands it in a folder of its own under `w4ve/plugins/`, where
  the loader finds it. It used to be refused outright, which was the honest
  answer while there was nothing to run one on.
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
- **A piece can own a path on the public side.** An `http` component says
  "serve this folder here" or "send this path to my service", and `w4ve http`
  writes the nginx or Caddy configuration for it. It gets right the four things
  that cost a night on a real machine: exact routes before prefixes, the body
  size limit, a cache header of its own (inherit the neighbour's and the
  browser downloads your images instead of showing them) and the trailing slash
  on an alias.
- **A piece can say what it writes to disk, and how much.** A `storage`
  component carries a quota and a retention rule, and the guardian keeps it
  inside them. `by = "atime"` is what a cache wants: the song played every day
  survives even when it is the oldest thing there. Removing a piece never
  deletes the data; it tells you where it is.
- **A piece can bring something that repeats.** A `timer` runs in the guardian
  rather than in cron, which means a run that fails says so once and then keeps
  count, and says it again when it starts working. The ShapeBoard scan was dead
  for three days and the only symptom was a board that stopped changing.
- **A piece can carry the half the players download.** `client` jars are all
  installed, not just the one for this server's Minecraft version, and w4ve
  writes a `latest.json` beside them with the version and sha256 of each. Serve
  that folder and the mod can check its own version without anybody uploading
  anything by hand, in an order that if reversed points everybody at a 404.
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

## Native plugins

A native plugin is a **process**, and what crosses between it and the core is a
line of JSON. That is [RFC-0003](docs/rfc/RFC-0003-workers-y-protocolo-de-plugins.md),
and it exists for one reason: a plugin that hangs, crashes or floods must not
be able to take the guardian with it, and nobody can promise that about code
running inside their own process.

```sh
w4ve dev new my-plugin          # scaffold one that already passes conformance
w4ve dev check my-plugin        # does it really speak the protocol
w4ve dev run my-plugin          # talk to it with no server and no guardian
w4ve dev env my-plugin          # build it a virtualenv of its own
w4ve dev lock my-plugin         # freeze that environment for a second machine
```

**A plugin's dependencies are its own.** W4VE never installs anything into its
own environment, so two plugins wanting different versions of the same library
is not a conflict to resolve: it is two environments. A `.venv` beside a plugin
is used automatically, whatever its manifest says, because the manifest is
written by whoever made the plugin and the environment by whoever installed it.

`w4ve dev env` is the one thing here that installs, and it is a person typing a
command about a plugin they chose, being shown what will be fetched and saying
yes. With a `requirements.txt` carrying hashes it installs with
`--require-hashes`, which refuses anything not pinned, transitive dependencies
included.

Plugins live in `w4ve/plugins/`, one folder each, and `w4ve run` starts them
alongside the server.

```python
from w4ve_plugin import Plugin

plugin = Plugin("hello", "1.0.0")

@plugin.command("!!hello", help="say hello")
def hello(source, args):
    return "hello, %s" % (args[0] if args else "world")

@plugin.on("player_joined")
def joined(payload):
    plugin.say("welcome, %s" % payload["player"])

plugin.run()
```

What the core promises, and what is tested against plugins written to break it
(`tests/badplugins/`, five of them, each aimed at a different way this goes
wrong):

- **A plugin that never answers produces an error, not a hang.** Every request
  has a deadline, and the test measures that the deadline is really enforced,
  not only that the message says so.
- **A plugin that crashes takes down its own process and nothing else.**
  Anybody waiting on it is told; the rest never notice. One that dies three
  times in five minutes is left alone, the same rule the server follows.
- **A plugin that cannot keep up loses events instead of growing memory.** The
  queue has a ceiling and it says so once, because the thread handing events
  over is also the one that learns when Minecraft died.
- **Running is not the same as alive.** A process that starts and never speaks
  is refused with those words.

**The protocol is the contract, not the language**, and that is checked rather
than asserted: `sdk/shell/example` is a POSIX shell script with no SDK and no
JSON library, and it passes the same conformance kit the Python SDK does. Both
run under one guardian and both answer their commands.

`sdk/conformance.py` is what decides whether an SDK is official: it starts the
plugin, speaks RFC-0003 at it and checks what comes back. It knows nothing
about Python.

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
moves. Both work with **no `w4ve.toml`**, on purpose: the honest moment to ask
"will my plugins run under this?" is while MCDR is still the thing running.

It asks the interpreter the plugins will really run under, finding a
virtualenv in the server folder by itself (`--python` to say where). MCDR is
installed in one, so asking this one instead reports as missing whatever is
sitting right there.

The migration has been walked end to end against **MCDReforged 2.15.7 really
installed**, a real 1.21 world and the real plugins: stop MCDR, `init`, `run`,
backup, `stop`, and then **start MCDR again and watch it list the backup W4VE
made**. [docs/cutover-del-mirror.md](docs/cutover-del-mirror.md) is what
happened, in the order it happened.

The sharpest thing that came out of it: the one plugin that fails to load fails
**identically under both**, with the same `AttributeError` on an API MCDR
removed versions ago. A plugin that does not work today will not work after the
cutover either, and that is not a regression.

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

### Replacing the guardian without stopping the world

The server already survived its guardian dying. What it did not survive was an
*upgrade*: the next guardian could only adopt what it found, and an adopted
server has no console, so updating the runtime cost a restart.

```sh
w4ve run --take-over        # take the running server from the guardian that has it
```

The two ends of the console move across the control socket with `SCM_RIGHTS`,
which duplicates them in the receiver, and the old guardian then stands down
without touching anything it was watching. **The pid never changes**, and that
is the assertion the test is built around.

- **It moves one console.** One socket message carries one array of
  descriptors, and pairing several back up with the right process is guesswork
  this refuses to do.
- **Finding nobody is not a failure.** It says so and starts normally, because
  an upgrade that refuses to boot is worse than one that restarts.
- **The inherited process is not our child**, so there is no exit code to read
  when it dies. The journal says `exit, code unknown` rather than inventing a
  zero.

## The local API

Something that is not the CLI can ask W4VE to do things. It listens on a **Unix
socket**, not a port, because a socket is a file with an owner and a mode and
that is a permission system the operating system already implements correctly.

```toml
[api]
enabled = true
# listen = "127.0.0.1:8790"   # a port instead. Then a token is required.
```

```sh
w4ve api token dashboard --scope read    # issue one, printed once
w4ve api tokens                          # names and scopes, never the tokens
w4ve api get status                      # ask a route from the command line

curl --unix-socket <server>/w4ve/run/api.sock http://localhost/v1/status
```

Player identity integrations use `w4ve api token wavechat --scope identity`.
That service scope can exchange paired-instance proofs and inspect/authorize
player sessions, but cannot read logs or stop servers. Pairing instances,
changing grants and blocking accounts require an `admin` token. Identity routes
require a token even when the Unix API's other local routes do not.

```python
from w4ve_client import W4VE

w4ve = W4VE("/path/to/server")
print(w4ve.running())
w4ve.command("say hello")
```

- **Scopes, and `read` is the floor.** A dashboard that can only look cannot
  stop a world by accident, and the way to guarantee that is not documentation.
  `read` < `write` < `admin`, cumulative.
- **Nothing reads private files on a client's behalf.** Every answer comes from
  asking the guardian, the same way the CLI does. A second reader of the same
  files is a second thing to keep in step.
- **It is an extra, and it behaves like one.** If it cannot listen, the
  guardian says so and the server starts anyway. A bug inside it is a 500, not
  a dead guardian.
- **`sdk/client/conformance.py`** stands up a fake W4VE and checks a client
  against it: that a read token really cannot stop a server, that a wrong token
  and an absent W4VE are told apart, and that nothing is being read off disk.

### Work that takes minutes

A backup is the operation people want to run from outside, and `!!pb make`
comes back four minutes later. So the request that asks for it gets an id
instead of an answer, and the client polls.

```python
job = w4ve.start_job("!!pb make nightly",
                     until=r"Backup .* created",   # the line that ends it
                     timeout=900)
print(w4ve.wait_for_job(job["id"])["lines"])
```

```sh
curl --unix-socket <server>/w4ve/run/api.sock -X POST \
     -d '{"command": "!!pb make", "until": "created"}' \
     http://localhost/v1/jobs                     # 202, with an id
curl --unix-socket <server>/w4ve/run/api.sock \
     http://localhost/v1/jobs/<id>                # running, done or failed
```

- **It subscribes before it sends.** The other order loses the first line of
  the answer, which for a short command is the whole answer.
- **`until` is what ends it, `timeout` is the backstop.** With no `until`, the
  deadline *was* the plan and what it collected is the answer; with one,
  reaching the deadline is a failure and says what it was waiting for.
- **Cancelling stops us listening and nothing else.** A backup cannot be
  un-asked, and the message says so rather than implying the work was undone.
- **Jobs live in memory and the last 50 are kept.** Surviving a restart would
  mean a second place where the truth about a server is written down, and
  there is already one.

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

Each of these is a test in `tests/test_promises.py`, because a guarantee
nobody checks is a comment. They break the same way every time: somebody adds
a small convenience, it is obviously fine in the case they are looking at, and
a line of this file quietly stops being true.


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
- **A package cannot run anything.** It places files and declares processes.
  `package.py` cannot reach `subprocess`, `eval` or a shell, so "install this
  piece" never means "run this stranger's script".
- **A secret is never in the toml or the journal.** It goes in a file at mode
  600 and nowhere else, and an RCON password is redacted before it can be
  logged.
- **The API is off, and local when it is on.** A Unix socket needs no token
  because the file mode is the permission; a port always needs one.
- **It never touches the world, and never accepts a licence for you.** Nothing
  anywhere writes an `eula.txt`.
- **A conflict needs versions on both sides.** Carpet TIS 1.82 does not boot
  with Lithium 0.13, but 1.64 with 0.13 is fine. Telling a healthy server it is
  broken is how a tool teaches people to ignore it.

## What it does not do yet

- Talk to more than one server at a time. One `w4ve.toml`, one server, which is
  what a piece shared by a whole network still needs.
- Run without Python in the container. The guardian is Python, so an image
  that is only Java (`yolks:java_21` and friends) cannot start it. An MCDR image
  already has both, because MCDR is Python too.
- Reload a web server by itself. `w4ve http` writes the configuration and tells
  you the two commands; running them as root is your call.

## Configuration

| Variable | Meaning |
|---|---|
| `W4VE_INDEX` | catalog path or URL (default: the published `index.json`) |
| `W4VE_SERVER` | server folder, instead of looking upwards from the current one |
| `NO_COLOR` | plain output |

MIT. Part of [W4VE](https://github.com/CodeW4VE), Ware 4 Vanilla Experience.
