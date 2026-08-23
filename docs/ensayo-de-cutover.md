# El ensayo de cutover, corrido con los plugins de verdad

**Fecha:** 21 de agosto de 2026
**Cómo:** `w4ve plugins --load` sobre una carpeta con los doce plugins reales
**Para:** M4, que pedía "una suite ejecutándose, no una tabla"

El [survey de superficie](mcdr-compat-surface.md) contó cuánto MCDR había que
reimplementar. Esto es la otra pregunta, la que de verdad decide: **de los
plugins que MineWave corre hoy, ¿cuántos arrancan sobre W4VE?**

## El número

**8 de 12 cargan. Los otros 4 no fallan por nada nuestro.**

| Plugin | Versión | Estado |
| --- | --- | --- |
| DayCountNBT | 2.1.1 | carga ⚠️ le falta `nbtlib` para funcionar |
| DigAll | 4.1.1 | carga |
| Here | 1.2.2 | carga |
| joinMOTD++ | 3.1.0 | carga |
| MultiRconAPI | 1.0.0 | carga |
| QuickBackupM | 1.10.1 | carga |
| StatsHelper | 7.5.1 (fork) | carga |
| TimedQBM | 1.1.0 | carga |
| ChatBridge | custom | falta `colorlog`, `pycryptodome` |
| PrimeBackup | 1.9.5 | faltan 6 paquetes, entre ellos `APScheduler` |
| Task | 2.3.6 | falta `parse` |
| WhereIs | 2.2.1 | falta el plugin `minecraft_data_api` |

Los cuatro que no cargan piden **dependencias de pip del propio plugin**, o
sea que bajo MCDR fallarían exactamente igual en esta máquina. La distinción
importa y el informe la hace explícita: *nuestro hueco* y *su dependencia* se
dicen con palabras distintas.

## Lo que se rompió por el camino, y qué lo destapó

Ningún fixture inventado da esto. Cada línea es un plugin real negándose.

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

El del serializer merece su párrafo: **solo aparecía en la segunda carga.** La
primera escribe los valores por defecto y funciona; la segunda lee el archivo
y devuelve diccionarios donde el plugin espera objetos. Un cutover que se
probara una sola vez lo habría dado por bueno, y habría reventado en el primer
reinicio.

## Cómo repetirlo

```sh
w4ve plugins            # qué hay, qué le falta a cada uno, y el pip install
w4ve plugins --load     # cargarlos de verdad, sin tocar el servidor
```

Hay copias locales de casi todos en `/mnt/games` (`MineWave/Xeri/plugins/`,
`Filezilla Shit/shipe/1.8/plugins/`, `Program files/Descargas/Minecraft/`), así
que el ensayo se corre sin bajar nada del servidor.

## Lo que este número no dice

- **Cargar no es funcionar.** Ocho arrancan; que `!!qb` haga un backup correcto
  contra un mundo real es otra prueba, y esa necesita un mundo real.
- **Falta ChatBridge de verdad.** Por el lado de MCDR es pequeño (4 nombres, 8
  métodos, y están todos), pero el plugin es la punta: detrás hay un daemon y
  un bot, tres procesos que se hablan.
- Sigue sin ensayarse en el **mirror**, que es el layout de verdad (MCDR,
  contenedor, `mods/` bajo `server/`).
