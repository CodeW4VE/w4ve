# RFC-0003: workers, y el protocolo que hablan

**Estado:** aceptado e implementado (`worker.py`, `sdk/`), verificado el 23 de agosto de 2026
**Fecha:** 23 de agosto de 2026
**Autoridad:** `W4VE-MANIFIESTO.md` sección 8, hito M3
**Depende de:** RFC-0001 (el guardián y la propiedad del proceso)

Este RFC define qué es un plugin nativo de W4VE, en qué se ejecuta y qué le
dice al núcleo. Es lo que separa "W4VE ejecuta plugins de MCDR" de "W4VE tiene
plataforma de plugins".

---

## 1. El problema

La compatibilidad con MCDR funciona, y tiene un techo que no se puede subir:
**los plugins corren dentro del proceso del guardián**. Es lo que MCDR hace, y
lo hereda quien lo imita. Las consecuencias son las que se ven en cualquier
servidor con plugins:

- un plugin que se queda esperando en una llamada de red **retrasa al que
  esté detrás**;
- un plugin que se cae con una excepción no capturada puede llevarse por
  delante al hilo que lo llamaba;
- un plugin con una fuga de memoria hace crecer al proceso que vigila el
  servidor, que es justo el que no puede morir;
- dos plugins que quieren versiones distintas de la misma librería **no pueden
  coexistir**, porque comparten intérprete.

Ninguno de esos cuatro se arregla con más cuidado al escribir plugins. Son
consecuencia de compartir proceso.

El criterio de salida de M3 lo dice sin rodeos: **plugins lentos, con crash y
con tormenta de eventos no bloquean al supervisor.** Eso no es una promesa que
se pueda hacer in-process.

## 2. Qué decide este RFC

1. **Un plugin nativo es un proceso.** No un hilo, no un módulo importado.
2. **Habla un protocolo de líneas JSON** por su entrada y salida estándar.
3. **El protocolo es el contrato, no el lenguaje.** Cualquier cosa capaz de
   leer una línea y escribir otra puede ser un plugin de W4VE.
4. **El núcleo nunca espera indefinidamente.** Cada petición tiene un plazo, y
   agotarlo es una respuesta, no un cuelgue.
5. **Los plugins de MCDR siguen donde están**, in-process y compatibles. Este
   RFC no los toca ni los deprecia.

## 3. Por qué líneas JSON por stdin/stdout

Se consideraron un socket Unix, gRPC y un puerto TCP.

**Se eligió stdin/stdout con una línea JSON por mensaje**, y las razones son
las mismas por las que este proyecto elige casi todo:

- **Cualquier lenguaje puede hablarlo hoy**, sin librería, sin generar código y
  sin instalar nada. Un plugin en shell es concebible, y eso es la prueba de
  que el listón está donde debe.
- **Se depura leyendo.** Un log del protocolo es texto que una persona entiende
  a las tres de la mañana, que es cuando hace falta.
- **El guardián ya sabe supervisar procesos con stdin y stdout**: es
  exactamente lo que hace con Minecraft desde el RFC-0001. No hay maquinaria
  nueva que mantener.
- **La muerte del proceso es la desconexión.** No hay que inventar un
  heartbeat para saber que el otro lado se fue: el descriptor se cierra.

El coste aceptado es que **stderr queda reservado para los logs** del plugin, y
que el rendimiento no es el de un canal binario. Para lo que un plugin de
servidor hace, no es el cuello de botella.

## 4. El protocolo

Una línea, un objeto JSON, terminado en `\n`. `id` correlaciona pregunta y
respuesta.

### Del plugin al núcleo

| `type` | Cuándo | Campos |
| --- | --- | --- |
| `hello` | primero, siempre | `plugin`, `version`, `protocol`, `capabilities` |
| `command` | al arrancar, para registrar | `name`, `help`, `permission` |
| `ready` | cuando ha terminado de arrancar | (ninguno) |
| `log` | cuando quiera | `level`, `message` |
| `say` | decir algo en el juego | `text`, `player` (opcional) |
| `call` | pedir algo al núcleo | `method`, `params` |
| `result` | respuesta a un `event` o `invoke` | `id`, `ok`, `value` |

### Del núcleo al plugin

| `type` | Cuándo | Campos |
| --- | --- | --- |
| `welcome` | tras el `hello` | `protocol`, `server`, `config`, `data_dir` |
| `event` | algo pasó | `id`, `event`, `payload` |
| `invoke` | alguien escribió su comando | `id`, `command`, `args`, `source` |
| `result` | respuesta a un `call` | `id`, `ok`, `value` o `error` |
| `shutdown` | se va a parar | `reason` |

**El handshake es obligatorio y va primero.** Un worker que no dice `hello` en
sus primeros segundos no está vivo, esté su proceso vivo o no: eso distingue
"arrancó" de "funciona", que es la misma distinción entre liveness y readiness
del RFC-0001.

**Y `hello` tampoco es estar listo.** El orden completo es `hello` → `welcome`
→ `command`\* → `ready`. Entre el saludo y el `ready` el plugin registra sus
comandos y hace lo suyo; sin ese último paso el núcleo devuelve el control en
cuanto oye el `hello`, y el primer comando llega antes de que el plugin haya
dicho que es suyo. Eso pasó de verdad, lo encontró la suite la primera vez que
le preguntó sus comandos a un plugin recién arrancado.

**La versión del protocolo se negocia en el handshake.** El núcleo dice cuál
habla; un worker que pide una que no existe se rechaza al arrancar y no a
mitad de una conversación.

## 5. Lo que el núcleo garantiza

- **Un worker lento no retrasa a nadie.** Cada uno tiene su hilo lector, y las
  peticiones al worker tienen plazo. Agotado el plazo, la respuesta es un error
  con esa palabra, y la vida sigue.
- **Un worker que se cae no se lleva nada.** Muere su proceso. El núcleo lo
  anota, se lo dice a quien esperaba, y el resto ni se entera.
- **Un worker que se cae repetidamente se deja en paz.** La misma regla que el
  servidor: tres muertes muy seguidas y no se reinicia más, porque reintentar
  contra algo roto es cómo un incidente se convierte en un bucle.
- **Una tormenta de eventos no crece sin fin.** La cola de cada worker está
  acotada. Llena, se descartan eventos y **se dice una vez**. Un worker que no
  puede seguir el ritmo es un problema suyo, y esa es exactamente la razón por
  la que no está dentro del guardián.

## 6. Lo que un plugin declara

`w4ve.plugin.toml`, junto al código:

```toml
[plugin]
id       = "mi-plugin"
name     = "Mi Plugin"
version  = "1.0.0"
protocol = 1

[run]
command = ["python3", "main.py"]

[config]
saludo = "hola"
```

Menos campos de los que el manifiesto enumera, a propósito: los que están son
los que el núcleo usa hoy. Un campo que no se lee es documentación que miente.

## 7. Dos cosas que sólo salieron de implementarlo

Se anotan porque son errores fáciles de repetir en cualquier SDK que hable
este protocolo, y el conformance kit los persigue por eso.

**Leer y ejecutar no pueden ser el mismo hilo.** Es lo natural de escribir y
funciona hasta que un handler le pregunta algo al núcleo: manda la pregunta y
se queda esperando la respuesta **en el hilo por el que la respuesta va a
llegar**. El plugin se queda conversando consigo mismo para siempre. El SDK
oficial lee en un hilo y ejecuta handlers en otro, en orden, que mantiene la
promesa de "uno a la vez" y permite esperar al núcleo.

**El núcleo tiene que contestar lo que un plugin le pregunta, también cuando
es un banco de pruebas.** El conformance kit no lo hacía, así que suspendía a
cualquier plugin que usara la API y aprobaba sólo a los que no la usan. Ahora
contesta, con respuestas obviamente falsas que lo dicen.

## 8. Lo que este RFC no hace

- **No aísla dependencias todavía.** Un worker de Python usa el intérprete que
  su `command` diga, y apuntar a un virtualenv es cosa de quien lo instala. El
  aislamiento con wheels bloqueadas y hashes de la sección 8.2 es posterior.
- **No limita recursos.** Un worker puede comerse la memoria de la máquina. Lo
  que sí garantiza es que se lleva por delante sólo lo suyo.
- **No define el companion mod.** Los eventos tipados desde Fabric son otro
  contrato, y va en su propio RFC.
- **No sustituye a los plugins de MCDR.** Conviven; son dos poblaciones con
  garantías distintas, y decirlo claro vale más que fingir que son lo mismo.
