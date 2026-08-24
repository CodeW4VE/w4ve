# RFC-0001: guardián, propiedad del proceso y ciclo de vida

**Estado:** aceptado e implementado (`runtime.py`), verificado el 19 de agosto de 2026
**Fecha:** 19 de agosto de 2026
**Autoridad:** `W4VE-MANIFIESTO.md` v1.3, tareas 20.5, 20.11 y 20.12
**Evidencia:** un inventario de solo lectura de cinco servidores MCDR en
producción (8 de agosto de 2026). No se publica: es el mapa operativo de una
red concreta. Lo que este RFC necesita de él está citado aquí.
**Reemplaza:** nada
**Depende de:** nada

Este RFC define quién es el dueño del proceso del servidor de Minecraft, cómo
se lo arranca, se lo observa, se le habla y se lo detiene, y qué pasa cuando
muere cualquiera de las partes. Es el primer contrato del runtime: sin esto,
W4VE sigue siendo un instalador de archivos.

---

## 1. El problema, medido

El inventario del 8 de agosto encontró esto en los cinco servidores de MineWave:

- La cadena real es `MCDR -> /bin/sh -c -> Java`. Hay un shell intermedio que no
  aporta nada y rompe la relación padre/hijo: las señales que MCDR manda van al
  shell, no siempre al Java.
- En `smp`, MCDR arrancó el 2 de agosto y Java se reinició después, por
  separado. O sea que el controlador y el proceso del juego **ya están
  desacoplados de hecho**, pero por accidente, sin estados explícitos ni forma
  de saberlo desde fuera.
- El único canal de comandos es el stdin del proceso, que solo tiene el padre.
  Si el padre muere, el servidor sigue vivo pero sordo.
- `plugins` corre Paper 1.20.1 con `bukkit_handler`, o sea que el sabor del
  servidor ya es una variable en producción, no una hipótesis.

Y hay un requisito que MCDR nunca tuvo y W4VE sí necesita: las piezas
interesantes del ecosistema (WaveChat, ShapeBoard, RegionCast, MankeList) no
son un jar suelto, llevan una segunda mitad que es un proceso o una tarea
periódica. Si el runtime solo sabe supervisar Java, esa mitad sigue viviendo en
crons sueltos, que es exactamente cómo el escaneo de ShapeBoard estuvo tres
días muerto sin que nadie se enterara.

## 2. Qué decide este RFC

1. Hay un **guardián**, un proceso chico y estable cuyo único trabajo es poseer
   procesos y sobrevivir. No parsea reglas de negocio, no carga plugins, no
   habla con la red.
2. Hay un **controlador**, que es todo lo demás (parseo de eventos, plugins,
   compatibilidad MCDR, API). Puede caer, actualizarse y volver **sin tocar
   Java**.
3. Los dos hablan por un **socket Unix** dentro del directorio del servidor. El
   CLI es solo otro cliente de ese socket.
4. El guardián supervisa **procesos, en plural**: el servidor de Minecraft es
   el primero, pero los servicios auxiliares de las piezas usan el mismo
   contrato.
5. El comportamiento propio de cada sabor de servidor vive en un **handler**
   (`fabric`, `paper`, `vanilla`), no repartido por el código.
6. El estado es **reconstruible desde el sistema operativo**. Los archivos son
   una caché, no la verdad.

## 3. Anatomía

```text
w4ve guard            proceso de vida larga, dueño de todo lo de abajo
├── java ...          el servidor, hijo directo, en su propio process group
├── <servicio>        segundas mitades de las piezas (mismo contrato)
└── control.sock      socket Unix, único punto de entrada

w4ve run|status|stop  clientes efímeros del socket
w4ve ctl              el controlador, cliente privilegiado y reiniciable
```

Reglas de la anatomía:

- **Nada de `/bin/sh -c`.** El guardián hace `exec` del Java directamente, con
  la línea de comandos ya partida en argumentos. Un shell intermedio te roba
  las señales y te miente en el árbol de procesos.
- **`setsid` y process group propio.** Así una señal al grupo alcanza a los
  hijos que el servidor haya lanzado, y un Ctrl-C en la terminal del operador
  no viaja al servidor por accidente.
- El guardián **no reinicia solo** salvo que la política lo diga (sección 8).

## 4. Estados, y ninguno más

El estado del servidor es uno de estos siete. Cualquier otra cosa que se quiera
comunicar es un atributo, no un estado nuevo.

| Estado | Qué significa | Se sale a |
| --- | --- | --- |
| `stopped` | no hay proceso, y es lo esperado | `starting` |
| `starting` | el proceso vive, todavía no dijo que está listo | `running`, `crashed` |
| `running` | el proceso vive y pasó el readiness del handler | `stopping`, `crashed` |
| `stopping` | se pidió parada limpia, se espera | `stopped`, `killed` |
| `killed` | no paró a tiempo y se lo forzó | `stopped` |
| `crashed` | murió sin que se lo pidiéramos | `starting`, `stopped` |
| `unknown` | el guardián arrancó y encontró algo que todavía no verificó | cualquiera |

`unknown` existe a propósito: es el estado honesto entre "el guardián arrancó"
y "confirmé qué es ese PID". Mentir ahí es lo que produce dobles arranques.

**Readiness no es liveness.** Liveness es que el PID existe. Readiness es que
el handler vio la línea de arranque completado. Un servidor generando el mundo
está `starting` durante minutos y está perfectamente sano. Un servidor colgado
en un deadlock está vivo y no está listo. Se reportan por separado, siempre.

## 5. Cómo se habla con el servidor

Tres canales, con jerarquía explícita:

1. **stdin del proceso**, que es el canal primario, lo posee el guardián y es
   el único que da todos los comandos de consola.
2. **RCON**, que es el respaldo. Existe en los cinco servidores, sirve cuando
   el guardián no es el dueño (servidor adoptado en caliente) y es el único
   canal para un `connected server` ajeno.
3. **El companion mod**, cuando exista, para eventos tipados. No es un canal de
   comandos, es la mejora del canal de eventos (sección 7).

Decisión: **el stdin lo mantiene abierto el guardián y no se expone como FIFO
en disco.** Un FIFO parece la solución obvia para "que otro le escriba después"
y es una trampa: cuando el último escritor cierra, el servidor recibe EOF en la
consola y se comporta de forma distinta según el sabor. Si el guardián muere,
el servidor queda temporalmente sordo por stdin y el respaldo declarado es
RCON, hasta que el guardián vuelve y readopta (sección 6).

## 6. Adopción y supervivencia

El guardián debe poder volver a la vida y encontrarse un servidor ya corriendo,
sea porque el guardián anterior murió, sea porque el servidor lo arrancó otra
cosa (hoy, MCDR).

- La identidad de un proceso **no es el PID**. Es el par `(pid, starttime)`,
  leído de `/proc/<pid>/stat` campo 22. Los PID se reciclan; el instante de
  arranque no.
- Al arrancar, el guardián lee `w4ve/run/state.json`, verifica ese par, y solo
  entonces decide entre `running` y `stopped`. Si el par no calza, el registro
  es basura de un arranque anterior y se descarta con una línea en el journal.
- Un servidor **adoptado en caliente** se marca `owned = false`: el guardián lo
  observa y le habla por RCON, pero no tiene su stdin y lo dice en `status` en
  vez de fingir control total. Se vuelve `owned = true` en el siguiente
  arranque hecho por W4VE.
- **Lock de instancia**: `w4ve/run/lock` con `flock`, un guardián por
  directorio de servidor. Dos guardianes sobre el mismo servidor es la forma
  más rápida de arrancarlo dos veces y corromper el mundo.
- `w4ve/run/journal.log`: una línea por transición, con marca de tiempo,
  estado anterior, estado nuevo y causa. Es lo que se lee cuando algo pasó a
  las 4 de la mañana.

**Actualizar W4VE sin reiniciar el servidor** es requisito, no lujo. El camino
es el traspaso de descriptores por el socket Unix (`SCM_RIGHTS`): el guardián
nuevo recibe del viejo los fds de stdin y stdout y toma el relevo. Se declara
aquí como la dirección; la primera implementación puede vivir sin ello mientras
el guardián sea pequeño y estable, y mientras el controlador (que es lo que se
actualiza seguido) ya sea reiniciable por diseño.

## 7. Handlers de sabor

Un handler responde cinco preguntas sobre un sabor de servidor:

```python
class Handler:
    name          # "fabric" | "paper" | "vanilla"
    def ready(line)      # ¿esta línea dice que terminó de arrancar?
    def stopping(line)   # ¿esta línea dice que se está apagando?
    def stop_command()   # qué se le manda por consola para pararlo
    def parse(line)      # -> evento tipado (chat, join, leave, muerte) o None
    def capabilities()   # qué puede y qué no puede W4VE en este sabor
```

`capabilities()` es la pieza que evita mentir. Ejemplo para Paper, decidido el
19 de agosto:

```json
{
  "flavor": "paper",
  "managed": true,
  "packages": "none",
  "companion": false,
  "rcon": true,
  "note": "Los plugins de Bukkit no se instalan desde el catálogo de W4VE."
}
```

Paper es **managed con capacidades reducidas**, no un `connected server`. El
runtime posee su proceso igual que el de Fabric, lo arranca, lo para, lo
diagnostica y lee su consola; lo que no hace es instalarle paquetes ni cargar
un companion mod, y eso se muestra en `status` en vez de disimularse. La razón
es concreta: si Paper se queda como observado, MCDR sobrevive para siempre en
el quinto servidor de MineWave, y matar esa dependencia es el punto del
proyecto. El conector genérico del manifiesto v1.3 queda para servidores de
terceros de verdad, no para uno propio.

El detalle de parseo por sabor (las líneas exactas, que difieren entre Paper
1.20.1 y Fabric 1.21) es material del RFC-0003, no de este.

## 8. Parada, crash y política

- **Parada limpia**: se manda el `stop_command()` del handler y se espera
  `stop_grace` (por defecto 90 segundos, configurable). Guardar un mundo grande
  tarda, y matar a un servidor mientras guarda es cómo se pierden chunks.
- Vencido el plazo: `SIGTERM` al process group, y a los 10 segundos `SIGKILL`.
  Cada escalón se escribe en el journal.
- **Crash**: muerte sin `stopping` previo. Se registra el código de salida y
  las últimas 50 líneas de consola en el journal.
- **Política de crash-loop**: tres muertes en menos de diez minutos y el
  guardián deja de reintentar, se queda en `crashed` y lo dice. Reintentar sin
  freno contra un mundo corrupto es cómo se convierte un incidente en una
  pérdida de datos.
- El reinicio automático es **opt-in por servidor**, nunca el default. Un
  reinicio silencioso esconde justo la información que hace falta.

## 9. Servicios auxiliares

Mismo guardián, mismo contrato, mismos estados. Un servicio declara:

```toml
[services.wavechat-media]
command = ["python3", "serve.py"]
cwd     = "services/wavechat"
health  = { http = "http://127.0.0.1:8130/health", every = "30s" }
restart = "on-failure"
```

Diferencias con el servidor de Minecraft: no tienen consola de comandos, su
readiness es un health check en vez de una línea de log, y su reinicio
automático sí es razonable por defecto (`on-failure`), porque un servicio caído
no arriesga un mundo.

Esto es lo que permite que una pieza compleja se instale de verdad. WaveChat
deja de ser "un mod más cuatro cosas que alguien montó a mano" y pasa a ser una
pieza con un servicio declarado que el runtime supervisa, mide y reinicia. El
formato `.w4ve` es quien traerá esa declaración; este RFC solo garantiza que
haya quien la ejecute.

## 10. Comandos que este RFC habilita

| Comando | Qué hace |
| --- | --- |
| `w4ve run` | arranca el guardián y el servidor; en primer plano por defecto |
| `w4ve start` / `w4ve stop` | arranca o para el servidor con el guardián ya vivo |
| `w4ve status` | estado, readiness, sabor, capacidades, `owned`, uptime, PID |
| `w4ve console` | se engancha a la consola en vivo y manda comandos |
| `w4ve logs` | vuelca consola y journal, con `--follow` |
| `w4ve doctor` | además de lo de hoy, diagnostica el runtime |

`w4ve run` en primer plano por defecto es a propósito: dentro de un contenedor
de Pterodactyl, el runtime **es** el proceso principal, y ahí un daemon que se
va al fondo es un estorbo. El modo servicio se declara con `--detach` y con la
unidad systemd de M2.

## 11. Qué NO cubre este RFC

- El protocolo de eventos tipados y el companion mod: RFC-0003.
- El formato del estado técnico, el lockfile y el journal de paquetes:
  RFC-0002.
- La compatibilidad con plugins de MCDR: es M4, y se apoya en este contrato.
- Métricas, jobs y ventanas de mantenimiento: M7.

## 12. Criterio de aceptación

Verificado el 19 de agosto de 2026 contra tres servidores: uno de mentira
(`tests/fakeserver.py`), un **Minecraft vanilla 1.21 de verdad** y el **jar
exacto de Paper 1.20.1 build 196 que corre `plugins`**. Las 16 pruebas de
`tests/test_runtime.py` levantan procesos reales, sin nada simulado.

Lo que falta para cerrarlo del todo está al final de la lista.

1. ✅ `w4ve run` arranca el servidor y `status` lo muestra pasar por `starting`
   y `running`, con readiness separado de liveness. Vanilla real: `starting`
   durante el arranque, `running  ready` al leer
   `[14:11:41] [Server thread/INFO]: Done (0.586s)! For help, type "help"`.
2. ✅ Matar el guardián con `SIGKILL` deja el servidor **vivo** (comprobado con
   Minecraft de verdad, pid 71221 sobrevivió), y el guardián siguiente lo
   readopta por `(pid, starttime)` y lo declara `adopted, no console` en vez de
   fingir control. Enviarle un comando por consola falla a propósito.
3. ✅ `w4ve stop` guarda y para limpio: el vanilla escribió
   `All dimensions are saved` y salió con código 0. Con un proceso que ignora
   `stop` y luego ignora `SIGTERM`, el escalón hasta `SIGKILL` queda escrito en
   el journal.
4. ✅ Tres muertes en menos de diez minutos y el guardián se queda en `crashed`
   sin volver a intentarlo.
5. ✅ Lo mismo, entero, contra Paper 1.20.1 build 196: detectado solo por la
   carpeta `plugins/` y el jar, leído su formato distinto de consola
   (`[14:15:06 INFO]: Done (3.047s)!`), parado limpio, y `status` diciendo
   `paper: Bukkit plugins are not installed from the W4VE catalog.`
6. ✅ Un servicio auxiliar declarado se registra, arranca y se reporta junto al
   servidor.
7. ✅ Nada de lo anterior necesita MCDR, ni el catálogo, ni red: los comandos
   del runtime no descargan el índice.

Pendiente para cerrar el RFC contra producción:

- Ensayarlo en el **mirror de MineWave**, que es el layout real (MCDR, Java
  dentro de un contenedor, `mods/` colgando de `server/`). Eso implica parar
  MCDR en ese servidor y es una decisión del operador, no del runtime.
- El **traspaso de descriptores** (`SCM_RIGHTS`) para actualizar el guardián
  sin reiniciar el servidor. Hoy el servidor sobrevive al guardián, pero
  recupera la consola solo en el siguiente arranque hecho por W4VE.
- ~~RCON como canal de respaldo real~~ ✅ **hecho el 19 de agosto**, en
  `rcon.py`: un servidor adoptado se para pidiéndoselo por RCON y solo se
  recurre a las señales si no contesta, se le pueden mandar comandos con la
  respuesta de vuelta, y `status` dice si hay RCON en vez de decir solo que no
  hay consola. Las credenciales se leen del `server.properties` del propio
  servidor cuando hacen falta: no se copian al estado de W4VE ni a sus logs, y
  hay una función de redacción para que una contraseña no llegue nunca a una
  línea de log. 13 pruebas, contra un servidor que habla el protocolo de
  verdad, incluidas las dos partes que todo el mundo falla la primera vez: una
  contraseña rechazada contesta con id -1, y una respuesta larga llega partida
  en varios paquetes sin nada que marque el final.
