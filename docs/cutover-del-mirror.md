# El cutover del mirror, paso a paso

**Estado:** escrito el 23 de agosto de 2026. **No ejecutado**: parar MCDR en el
mirror es una decisión que no es mía.

El mirror es el sitio correcto para hacerlo primero: tiene el layout de verdad
(MCDR, contenedor, `mods/` bajo `server/`) y no hay nadie jugando. Este
documento existe para que el día que se haga no haya que pensar, sólo leer.

## Lo que ya está probado, y dónde

| Lo que da miedo | Ensayado como |
| --- | --- |
| que un plugin no cargue | los 13 del smp, con sus copias locales |
| que cargue y no funcione | backup real, eventos entre plugins, hot reload |
| que el layout de MCDR lo despiste | una copia del layout, aquí abajo |
| que se pierdan los permisos | el mismo `permission.yml`, leído tal cual |
| que ChatBridge se quede fuera | sus cuatro procesos, corriendo sin MCDR |

El ensayo del layout se hizo con `game_dir = "server"`, `plugins/` y `config/`
colgando de la raíz y un `permission.yml` de MCDR sin tocar. Resultado:
**QuickBackupM hizo un backup real sin cambiarle una línea de config**, porque
su `server_path: ./server` por defecto es justo el correcto ahí; los configs se
escribieron en `<raíz>/config/`, donde MCDR los tiene; y `!!w4ve permission
list` contestó con la lista de siempre.

## Antes de tocar nada

1. Copia de seguridad del mundo, con lo que ya haya (`!!qb make antes-de-w4ve`).
2. Anotar el comando de arranque exacto que MCDR usa hoy, de su `config.yml`
   (`start_command`), y su `working_directory` (casi siempre `server`).
3. `w4ve plugins` en la carpeta del mirror, **sin cargar nada**: dice qué hay,
   qué le falta a cada plugin y la línea de `pip install` exacta.
4. Instalar esas dependencias. Hoy, medido sobre los plugins del smp:
   `APScheduler SQLAlchemy colorlog nbtlib parse pathspec psutil pycryptodome
   pytz requests xxhash zstandard`. **`mcdreforged` no está en esa lista y no
   debe estarlo.**
5. `w4ve plugins --load`: el ensayo en frío, contra nada. Si aquí falla algo, no
   se sigue.

## El cutover

1. Parar MCDR. **Parar, no desinstalar.** Nada de lo que sigue mueve un archivo
   suyo: `plugins/`, `config/`, `permission.yml` y `server/` se quedan donde
   están, y por eso la vuelta atrás es volver a arrancarlo.
2. `w4ve init --name mirror --mc <versión>`, y en el `w4ve.toml`:
   - `game_dir = "server"`
   - `start = "<el comando que decía MCDR>"`
   - `java`, si el contenedor no usa el del sistema.
3. `w4ve run`. En un contenedor eso es el proceso principal: el guardián **es**
   el proceso, y no hay un wrapper dentro de otro wrapper como hoy.
4. Comprobar, en este orden, que es el orden en que duele si falla:
   - `w4ve status`: el servidor corriendo y listo.
   - `!!help`: la ayuda de todos los plugins, en palabras y no en claves.
   - `!!w4ve permission list`: la lista de staff de siempre.
   - `!!qb make prueba-w4ve` y mirar que el slot pesa lo que tiene que pesar.
     ⚠️ **Un backup que dice "ok" y pesa cuatro kilobytes está vacío.**
   - `w4ve stop` y ver el mundo guardarse en la consola.

## La vuelta atrás

Arrancar MCDR otra vez. Eso es todo, y es así por diseño: W4VE no mueve los
archivos de nadie. Lo único que queda suyo en la carpeta es `w4ve.toml` y la
carpeta `w4ve/` con su estado, y ninguna de las dos molesta a MCDR.

Lo único que sí cambia debajo son los archivos que los plugins escriben ellos
mismos (`config/`, `qb_multi/`), y los escriben igual bajo los dos.

## Lo que no está probado y hay que mirar con los ojos abiertos

- **El contenedor.** El guardián en primer plano dentro de wings, con el `chown`
  previo al arranque que ya dio un disgusto una vez (ver el gotcha del bind
  read-only). Se arranca desde el panel, no a mano.
- **El bot de ChatBridge contra Discord de verdad.** Todo lo demás de ChatBridge
  está probado; el login con un token real, no.
- **`!!online` desde la consola** no funciona, ni aquí ni bajo MCDR: el plugin
  pide el jugador que escribió el comando. Desde el chat sí.
- **Los plugins que sólo existen en el servidor.** El survey (`tools/mcdr_api_survey.py`)
  los lee sin instalarlos, y conviene pasarlo por la carpeta del mirror antes,
  por si hay alguno que no estaba en las copias locales.
