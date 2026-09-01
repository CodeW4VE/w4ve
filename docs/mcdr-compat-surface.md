# Cuánto MCDR hay que reimplementar, medido

**Fecha:** 19 de agosto de 2026, **completado el 1 de septiembre**
**Método:** `tools/mcdr_api_survey.py`, lectura estática con `ast`, sin ejecutar nada
**Alcance:** los ocho plugins de aceptación del manifiesto, ya sin huecos
**Para:** M4, compatibilidad MCDR

El manifiesto acepta reemplazar MCDReforged con una condición: que los plugins
que MineWave usa sigan funcionando. Hasta hoy el tamaño de ese trabajo era una
opinión. Esto lo cuenta.

## El número

Para correr los plugins medidos, la capa de compatibilidad tiene que ofrecer:

| Qué | Con seis plugins | **Con los ocho** |
| --- | ---: | ---: |
| Nombres importables desde `mcdreforged` | 41 | **58** |
| Métodos del `ServerInterface` | 23 | **28** |
| Eventos de MCDR | 9 | **9** |

La columna de la derecha es de la segunda medición, con los tres que faltaban.
**Los eventos no se movieron**, y eso es el hallazgo: la superficie de eventos
ya estaba entera con seis plugins, así que los tres que faltaban no podían
traer un evento nuevo por mucho que se retrasaran.

De los 23 métodos, **12 los pide más de un plugin** y son el núcleo:

```
execute                    get_data_folder            get_plugin_command_source
get_self_metadata          is_on_executor_thread      is_rcon_running
load_config_simple         rcon_query                 register_command
register_help_message      save_config_simple         say
```

Los otros 11 los pide uno solo, y casi todos son PrimeBackup: `broadcast`,
`get_mcdr_config`, `get_plugin_instance`, `has_translation`,
`is_server_running`, `is_server_startup`, `schedule_task`, `start`, `stop`,
`tell`, `wait_until_stop`.

Los 9 eventos son toda la superficie de eventos que estos plugins usan:

```
on_load (6)   on_info (4)          on_player_joined (4)   on_unload (4)
on_player_left (3)   on_server_startup (2)   on_server_stop (2)
on_server_start (1)  on_user_info (1)
```

## Qué se midió

| Plugin | De dónde | Nota |
| --- | --- | --- |
| PrimeBackup | `CodeW4VE/PrimeBackup` | el más exigente con diferencia: usa 34 de los 41 nombres |
| ChatBridge | `CodeW4VE/ChatBridge` | el único que usa `mcdreforged.api.rcon` y `on_user_info` |
| StatsHelper | `CodeW4VE/StatsHelper` | fork nuestro |
| DigAll | `CodeW4VE/DigAll` | repo propio |
| Here | `TISUnion/Here` | uno de los `.py` de un archivo del smp |
| MinecraftDataAPI | `Fallen-Breath/MinecraftDataAPI` | dependencia real de WhereIs |
| WhereIs | `plugins/` del smp | traducido, y por eso el que más métodos suma |
| Online | `plugins/` del smp | pequeño: cuatro métodos, dos eventos |
| MultiRconAPI | `plugins/` del smp | el segundo que usa `mcdreforged.api.rcon` |

**Los tres que faltaban, medidos el 1 de septiembre.** WhereIs, Online y
MultiRconAPI no aparecen en GitHub con una búsqueda directa, así que se leyeron
de donde sí están: el `plugins/` del smp, copiados tal cual y abiertos desde
dentro sin instalarlos (`WhereIs-v2.2.2.mcdr`, `Online-v1.4.0.mcdr`,
`MultiRconAPI-V2.0.1.pyz`).

Suman **cinco métodos nuevos** y ni un evento:

```
get_mcdr_language        WhereIs
get_plugin_metadata      WhereIs
register_event_listener  WhereIs
register_translation     WhereIs
tr                       Online, WhereIs
```

**Los cinco ya estaban implementados** en la capa de compatibilidad, o sea que
completar la medición no añadió trabajo: confirmó que no había ninguno
escondido. Los tres son plugins pequeños y bien educados; el que marca el
tamaño del trabajo sigue siendo PrimeBackup.

Que WhereIs pida cuatro de los cinco tiene sentido y no es casualidad: es el
único de los ocho que está traducido, y cuatro de esos métodos son el
andamiaje de las traducciones.

## Tres cosas que salieron de medir en vez de suponer

1. **El primer conteo dijo 28 eventos.** Eran nueve: los otros 19 son funciones
   `on_algo` de los propios plugins, callbacks suyos que MCDR nunca llama.
   Contarlas habría inflado el trabajo un 200 %.
2. **ChatBridge tiene una clase `server` propia.** Contar `server.tell(...)` en
   sus módulos internos como API de MCDR sumaba métodos que no existen en el
   `ServerInterface`. Ahora solo se leen los archivos que importan
   `mcdreforged`, y la cuenta bajó de 28 a 23 métodos.
3. **`load_config_simple`, `register_command` y `register_help_message` los usan
   los seis.** Si la compatibilidad empieza por tres funciones, son esas.

## Lo que este número no dice

- Es un **suelo**, no un techo. Una llamada hecha a través de una variable
  (`api = server.get_plugin_instance(...)`, y después `api.algo()`) no se
  atribuye al interface, y `getattr` es invisible para una lectura estática.
- Contar un nombre no es implementarlo. `RText` aparece una vez en la lista y
  detrás lleva un árbol de estilos, colores, eventos de click y traducción.
  `register_command` lleva detrás el árbol de Brigadier entero.
- Y lo que de verdad decide si un plugin corre no es la lista, es el
  comportamiento: los hilos, el orden de los eventos y qué pasa cuando uno
  falla. Por eso el criterio de M4 es una suite ejecutándose, no una tabla.

## Cómo repetirlo

```sh
python3 tools/mcdr_api_survey.py ~/plugins/*              # una línea por API
python3 tools/mcdr_api_survey.py --by-plugin ./PrimeBackup
python3 tools/mcdr_api_survey.py --json ./DigAll | jq .
```

Sirve también contra los plugins instalados en un servidor, sin sacarlos de
ahí: lee `.mcdr` y `.pyz` desde dentro y no ejecuta una sola línea de ellos.
