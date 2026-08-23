# El ensayo de cutover, corrido con los plugins de verdad

**Fecha:** 21 de agosto de 2026, ampliado el 23 de agosto
**Cómo:** `w4ve plugins --load` sobre una carpeta con los plugins reales, y
después los mismos plugins corriendo bajo el guardián con un servidor 1.21
**Para:** M4, que pedía "una suite ejecutándose, no una tabla"

El [survey de superficie](mcdr-compat-surface.md) contó cuánto MCDR había que
reimplementar. Esto es la otra pregunta, la que de verdad decide: **de los
plugins que MineWave corre hoy, ¿cuántos arrancan sobre W4VE?**

## El número

**13 de 13 cargan, sin un solo problema.** El 21 de agosto eran 8 de 12.

| Plugin | Versión | Estado |
| --- | --- | --- |
| ChatBridge | 2.5.3 (custom) | carga |
| DayCountNBT | 2.1.1 | carga |
| DigAll | 4.1.1 | carga |
| Here | 1.2.2 | carga |
| joinMOTD++ | 3.1.0 | carga |
| MinecraftDataAPI | 1.6.1 | carga |
| MultiRconAPI | 1.0.0 | carga |
| PrimeBackup | 1.9.5 | carga |
| QuickBackupM | 1.10.1 | carga |
| StatsHelper | 7.5.1 (fork) | carga |
| Task | 2.3.6 | carga |
| TimedQBM | 1.1.0 | carga |
| WhereIs | 2.2.1 | carga |

Lo que cambió entre una cifra y otra tiene dos mitades, y conviene no
confundirlas:

- **Las dependencias de pip se instalaron.** Los cuatro que faltaban el 21 no
  fallaban por nada nuestro; pedían paquetes suyos. En un venv con
  `APScheduler SQLAlchemy colorlog nbtlib parse pathspec psutil pycryptodome
  pytz requests xxhash zstandard hjson`, más MinecraftDataAPI (que es otro
  plugin, no un paquete), el diagnóstico llega hasta el final.
  ⚠️ `psutil` y `requests` no estaban en la lista del 21: **no se veían**,
  porque los plugins que los piden morían antes de que nadie preguntara.
- **Y entonces aparecieron diez huecos nuestros**, que hasta ese día estaban
  tapados por las dependencias que faltaban.

## Lo que se rompió por el camino, y qué lo destapó

Ningún fixture inventado da esto. Cada línea es un plugin real negándose.

### 21 de agosto

| Qué faltaba | Quién lo destapó |
| --- | --- |
| `lang/` dentro de un `.mcdr` no se leía nunca | DigAll contestaba `dig_all.command.sidebar_set` |
| YAML sin bloques `key: \|` | StatsHelper contestaba `!!stats` con un `\|` |
| `preferred_language_context()` | StatsHelper, que envuelve su ayuda en él |
| `LiteralEvent` y los eventos entre plugins | QuickBackupM y TimedQBM, que se hablan por ahí |
| `RconConnection` como clase | MultiRconAPI hace `class Rcon(RconConnection)` |
| `MCDReforgedLogger.set_file()` | Task, al cargar |
| `as_plugin_server_interface()` | Task, a nivel de módulo |
| `Metadata.get_description(language)` | TimedQBM |
| `mcdreforged.command.command_source` vacío | Task |
| El serializer no entraba en `Dict[str, SlotInfo]` | QuickBackupM, **solo al recargar** |
| `failure_message_getter` llamado con un argumento de más | cualquier `requires(pred, lambda: ...)` |
| `on_player_joined` / `on_player_left` no se emitían | 4 de los 6 plugins medidos |

### 23 de agosto

| Qué faltaba | Quién lo destapó |
| --- | --- |
| `event_listener` no existía | ChatBridge, que decora una función de módulo |
| Un listener registrado durante el import se perdía en silencio | el mismo decorador: el plugin todavía no está en `loaded` |
| `SyncStdoutStreamHandler` | ChatBridge, que lo mete en su propio logger |
| `Serializable.get_default()` | Task y WhereIs, los dos al cargar |
| `psi()` devolvía la interfaz compartida, no la del plugin | PrimeBackup, que le pide `get_self_metadata()` |
| `Metadata` no estaba en `mcdreforged.api.all` | PrimeBackup, que lo importa para tipar |
| Un enum se escribía por valor y no por nombre | PrimeBackup: su `HashMethod` guarda un dataclass |
| `re.Pattern` no se sabía ni escribir ni leer | MinecraftDataAPI, que declara así sus parsers |
| La `deserialize` de módulo llamaba al classmethod | Task la llama desde el suyo: `RecursionError` |
| El argumento se llamaba `target_class` y no `cls` | Task lo pasa por nombre |
| Un campo sin default se escribía como `null` | WhereIs, que luego no se aprueba a sí mismo |
| `RTextBase.join` | Task, que arma con él la pantalla de `!!task` |
| `to_plain_text()` podía devolver algo que no era texto | Task otra vez, ya en la consola |
| `Logger.unset_file()` | Task, en `on_unload` |
| **`on_server_stop` se despachaba sin el código de salida** | ChatBridge, PrimeBackup y Task, los tres a la vez |

Dos merecen su párrafo.

**El del serializer** (21-ago) sólo aparecía en la segunda carga. La primera
escribe los valores por defecto y funciona; la segunda lee el archivo y
devuelve diccionarios donde el plugin espera objetos. Un cutover que se
probara una sola vez lo habría dado por bueno.

**El de `on_server_stop`** sólo aparecía al apagar. MCDR pasa el código de
salida como segundo argumento y todos los plugins lo declaran obligatorio, así
que los tres se rompían **a la vez y en el peor momento**: `on_server_stop` es
donde un plugin guarda lo que tenía en la mano. El fixture de nuestros tests
declaraba `on_server_stop(server)`, con un argumento, así que la suite pasaba
en verde. Ya declara los dos.

## Cargar no es funcionar: lo que se probó contra un mundo real

Servidor Minecraft 1.21 de verdad (el jar del smp), mundo generado, RCON
encendido, **los 13 plugins corriendo dentro del guardián**:

- `!!help` mezcla la ayuda de todos, en palabras y no en claves.
- **QuickBackupM hizo un backup real del mundo** (`!!qb make`), con su
  `info.json` y su comentario. Se ve el ciclo entero: apaga autosave, guarda,
  detecta "Saved the game", copia, reactiva.
- **TimedQBM se enteró del backup de QuickBackupM** ("New backup detected,
  timer reseted"): los eventos entre plugins funcionan de verdad.
- Recarga en caliente de un plugin, con su config editada a mano, y el
  siguiente comando ya usa la config nueva.
- `!!task`, `!!stats`, `!!dig-all status`, `!!w4ve plugin list` responden.
- Parada limpia: mundo guardado, `on_unload` de cada plugin, sin un error.
- `!!MCDR` contesta la línea de cortesía y no gestiona nada.

⚠️ **Un bug de la consola que sólo se ve usándola**: `w4ve console` imprimía
cada respuesta **dos veces**, porque le llegaba por el socket y por el stream
al que ya estaba atada. Con `!!help` salían las dos copias intercaladas y el
título en medio.

## Cómo repetirlo

```sh
w4ve plugins            # qué hay, qué le falta a cada uno, y el pip install
w4ve plugins --load     # cargarlos de verdad, sin tocar el servidor
```

Hay copias locales de casi todos en `/mnt/games` (`MineWave/Xeri/plugins/`,
`Filezilla Shit/shipe/1.8/plugins/`, `Program files/Descargas/Minecraft/`), así
que el ensayo se corre sin bajar nada del servidor.

## Lo que este número no dice

- **ChatBridge carga y arranca su cliente, pero el daemon y el bot no están.**
  Detrás del plugin hay tres procesos que se hablan, y de eso sólo se ha
  probado la punta: el cliente intenta conectar, y falla porque no hay nadie
  al otro lado. Eso sigue siendo lo que de verdad falta para el cutover.
- **PrimeBackup arranca deshabilitado por su propia config**, que es su
  comportamiento normal la primera vez. No se ha probado un backup suyo.
- Sigue sin ensayarse en el **mirror**, que es el layout de verdad (MCDR,
  contenedor, `mods/` bajo `server/`).
- `!!day` y `!!here` contestan en chino porque DayCountNBT y Here 1.2.2 **no
  traen inglés**. Eso no es nuestro.
