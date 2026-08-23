# El cutover, paso a paso

**Estado:** ejecutado de principio a fin el 23 de agosto de 2026, sobre una
réplica del layout del mirror con **MCDReforged 2.15.7 de verdad instalado**, un
Minecraft 1.21 real y los plugins reales del servidor. Ida y **vuelta**.

Lo que sigue no es un plan: es lo que pasó, en el orden en que pasó. Cada paso
se ejecutó tal como está escrito, y lo que el documento no decía se descubrió
por las malas y está corregido abajo.

En el mirror de verdad falta una cosa que aquí no se pudo probar: **el
contenedor**. Va al final, en lo que hay que mirar con los ojos abiertos.

---

## Lo que quedó demostrado

| Lo que da miedo | Cómo se comprobó |
| --- | --- |
| que un plugin no cargue | 13 de 13 cargan bajo el guardián |
| que W4VE sea peor que MCDR | **el único plugin que falla, falla igual bajo los dos, con el mismo error** |
| que cargue y no funcione | backup real de 280K, hecho por QuickBackupM bajo W4VE |
| que se pierdan los permisos | `permission.yml` de MCDR leído tal cual: owner y admin correctos |
| que el mundo no se guarde al parar | "All dimensions are saved" en la consola |
| **que no se pueda volver atrás** | **MCDR arrancó otra vez y vio el backup que hizo W4VE** |

Ese último es el que hace que todo esto sea seguro de intentar, y es el que
nunca se había probado.

### La comparación que más dice

`SimpleOP.py` falla al cargar. Bajo MCDR 2.15.7:

```
AttributeError: 'PluginServerInterface' object has no attribute 'add_help_message'
```

Bajo W4VE, sobre el mismo archivo, palabra por palabra lo mismo. No es que W4VE
no lo soporte: es que ese plugin usa una API que MCDR quitó hace versiones.
**Un plugin que no funciona hoy tampoco va a funcionar después del cutover, y
eso no es una regresión.**

---

## Antes de tocar nada

1. **Copia de seguridad del mundo**, con lo que ya haya: `!!qb make antes-de-w4ve`.
2. **`w4ve plugins`** en la carpeta del servidor. **Funciona sin `w4ve.toml`**,
   a propósito: el momento honesto de preguntar "¿mis plugins van a correr?" es
   mientras MCDR sigue siendo lo que corre.
   - Encuentra solo el virtualenv del servidor (`venv/`, `.venv/`, `env/`) y
     **pregunta a ESE Python** qué hay instalado. ⚠️ Es la diferencia entre la
     verdad y una lista de paquetes que sí están: MCDR se instala en un venv, y
     preguntarle al Python del sistema dice que falta todo.
   - Si el intérprete está en otro sitio: `w4ve plugins --python /ruta/al/python`.
3. **Instalar lo que diga que falta.** Da la línea de `pip install` exacta.
   ⚠️ **`mcdreforged` no está en esa lista y no debe estarlo.**
4. **`w4ve plugins --load`**: el ensayo en frío, contra nada. Si aquí falla algo
   que no falle también bajo MCDR, no se sigue.
   - ⚠️ Un plugin puede fallar aquí por una ruta relativa (`config/loquesea.json`)
     y funcionar luego perfectamente: en el ensayo el directorio de trabajo no
     es la raíz del servidor, y bajo el guardián sí lo es. `CrashRestart` hace
     exactamente eso.

---

## El cutover

1. **Parar MCDR. Parar, no desinstalar.** Nada de lo que sigue mueve un archivo
   suyo: `plugins/`, `config/`, `permission.yml`, `qb_multi/` y `server/` se
   quedan donde están. Por eso la vuelta atrás es volver a arrancarlo.

2. **`w4ve init --name <nombre> --mc <versión>`.**
   Lee el `config.yml` de MCDR y **copia solo dos cosas**: el
   `working_directory` (que se convierte en `game_dir`) y el `start_command`
   tal cual, sin reescribirlo. Lo dice al terminar:

   ```
   ok  wrote /ruta/w4ve.toml
       game_dir = "server", because MCDR's config.yml says so
       start command copied from MCDR's config.yml
   ```

   ⚠️ Si no sale esa segunda línea, **para y mira por qué**. Un `game_dir` mal
   puesto es el fallo silencioso de todo esto: los plugins escriben en rutas
   relativas, y un backup acaba en una carpeta vacía diciendo que fue bien.

3. **`w4ve run`.** En un contenedor eso es el proceso principal: el guardián
   **es** el proceso, y se acabó el wrapper dentro de otro wrapper.

4. **Comprobar, en este orden, que es el orden en que duele si falla:**

   | Qué | Qué tiene que salir |
   | --- | --- |
   | `w4ve status` | `server running ready` |
   | `w4ve plugins status` | los plugins que esperabas, cargados |
   | `!!help` | la ayuda de todos, **en palabras y no en claves** |
   | `!!w4ve permission list` | tu lista de staff de siempre |
   | `!!qb make prueba-w4ve` | y **mirar el tamaño del slot** |
   | `w4ve stop` | "All dimensions are saved" en la consola |

   ⚠️ **Un backup que dice "ok" y pesa cuatro kilobytes está vacío.** Es el
   síntoma de un `game_dir` equivocado, y se ve mirando `qb_multi/slotN`.

---

## La vuelta atrás

**Arrancar MCDR otra vez. Eso es todo.** Probado: arranca, carga sus plugins y
`!!qb list` muestra el backup que hizo W4VE, con su comentario, listo para
restaurar. Los dos escriben lo mismo en los mismos sitios porque **es el mismo
código de plugin haciendo el trabajo**.

Lo único de W4VE que queda en la carpeta es `w4ve.toml` y `w4ve/`, y MCDR no
mira ninguno de los dos.

---

## Lo que no está probado, y hay que mirar con los ojos abiertos

- **El contenedor.** El guardián en primer plano dentro de wings, con el `chown`
  previo al arranque que ya dio un disgusto una vez (el gotcha del bind
  read-only). Se arranca desde el panel, no a mano. Es lo único grande que queda.
- **El bot de ChatBridge contra Discord de verdad.** Sus cuatro procesos corren
  sin MCDR; el login con un token real, no se ha hecho.
- **`!!online` desde la consola** no funciona, ni aquí ni bajo MCDR: el plugin
  pide el jugador que escribió el comando. Desde el chat sí.
- **Los plugins que sólo existen en el servidor.** `tools/mcdr_api_survey.py`
  los lee sin instalarlos; conviene pasarlo por la carpeta antes, por si hay
  alguno que no estaba en las copias locales.
