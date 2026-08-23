# Hacer una pieza que se instale en un comando

Una pieza de W4VE es un `.w4ve`: un zip con un `w4ve.json` dentro. Esta guía es
para el caso difícil, el que hoy no se puede instalar de ninguna manera: una
pieza cuya segunda mitad no es un archivo, sino un proceso, una carpeta que
crece, una ruta pública y algo que hay que repetir cada minuto.

Todo lo que sale aquí está probado. Si algo no funciona como dice esta página,
la página está mal.

---

## 1. El esqueleto

```
mi-pieza/
  w4ve.json          <- el manifiesto, lo único obligatorio
  mod/               <- los jars, si los hay
  client/            <- los jars que bajan los jugadores, si los hay
  service/           <- el código del proceso, si lo hay
  config/            <- los archivos que el operador va a editar
```

```sh
w4ve pack mi-pieza            # -> mi-pieza-1.0.0.w4ve
w4ve install ./mi-pieza-1.0.0.w4ve
```

`pack` se niega a construir un paquete que miente: si el manifiesto nombra un
archivo que no está en la carpeta, lo dice y no escribe nada.

---

## 2. El manifiesto, campo por campo

```json
{
  "schema": 1,
  "id": "mi-pieza",
  "name": "Mi Pieza",
  "version": "1.0.0",
  "side": "server",
  "summary": "Una frase. Sale en `w4ve info`.",
  "guide": [
    "Apunta un registro DNS a esta máquina.",
    "Después: w4ve run"
  ],
  "settings": [ ... ],
  "components": [ ... ]
}
```

- **`id`**: minúsculas, dígitos y guiones. Es el nombre por el que se instala,
  se configura y se quita.
- **`side`**: `server`, `client` o `both`. Instalar una pieza de cliente en un
  servidor se rechaza al instalar, no al arrancar.
- **`guide`**: tres o cuatro líneas que se imprimen **cuando termina la
  instalación**, que es el único momento en que alguien las va a leer.

---

## 3. `settings`: lo que la pieza necesita que le digan

Esta es la parte que decide si alguien que no eres tú puede instalar tu pieza.

```json
"settings": [
  {
    "name": "public_url",
    "type": "url",
    "required": true,
    "title": "Dirección pública",
    "help": "Por dónde llegan los clientes. Hace falta un DNS apuntando aquí.",
    "example": "https://chat.ejemplo.com"
  },
  {
    "name": "discord_token",
    "type": "secret",
    "required": true,
    "title": "Token del bot de Discord",
    "help": "De la pestaña Bot de tu aplicación. No es el client secret."
  },
  {
    "name": "discord_guild",
    "type": "text",
    "discover": "discord.guild",
    "title": "Servidor de Discord",
    "help": "En cuál escribe el bot. W4VE los lista, se elige por nombre."
  },
  {
    "name": "music",
    "type": "bool",
    "default": false,
    "title": "Música",
    "help": "Apagada por defecto: cuesta disco."
  },
  {
    "name": "music_quota",
    "type": "text",
    "default": "2G",
    "when": "music",
    "title": "Tope de la caché de música",
    "help": "Sólo se pregunta si la música está encendida."
  }
]
```

**Tipos**: `text`, `url`, `int`, `bool`, `path`, `choice` (con `options`),
`list`, `secret`.

**Las cuatro reglas que hacen que esto funcione:**

1. **`help` es obligatorio**, porque el `help` es lo que acaba escrito como
   comentario encima de la línea en el `w4ve.toml` del operador. Sin él, la
   persona abre su archivo y ve un valor sin saber qué hace.
2. **Un `secret` nunca lleva `default`** y nunca se escribe en el `w4ve.toml`:
   va a `w4ve/secrets.json`, modo 600.
3. **`discover` en vez de pedir un id.** `discord.guild`, `discord.channel` y
   `discord.role` hacen que W4VE se conecte con el token que ya tiene, liste lo
   que hay y la persona elija por nombre. Un id de dieciocho dígitos copiado a
   mano es donde la gente abandona.
4. **`when`** encadena preguntas: lo que sólo importa si otra cosa está
   encendida no se pregunta.

### Cómo lo lee tu proceso

Todo lo respondido llega al proceso como variables de entorno, en mayúsculas y
con prefijo:

```python
import os
PUBLIC_URL = os.environ["W4VE_PUBLIC_URL"]
TOKEN      = os.environ["W4VE_DISCORD_TOKEN"]
MUSIC      = os.environ.get("W4VE_MUSIC") == "true"
```

Por eso casi ninguna pieza necesita plantillas de configuración. Si aun así lo
necesitas en un sitio donde una variable de entorno no vale (dentro de una
línea de comando, por ejemplo), se escribe `${setting:public_url}` o
`${secret:discord_token}` y W4VE lo sustituye al arrancar.

⚠️ **Un servicio con una referencia sin resolver no arranca**, y el error dice
qué falta. Arrancar con el token vacío falla mucho más tarde y de forma que
nadie puede leer.

---

## 4. `components`: las mitades de la pieza

### `mod` y `client`

```json
{"kind": "mod", "file": "mod/mipieza-1.0.0+1.21.jar", "mc": ">=1.21 <1.21.2",
 "reload": "restart"}
```

Un paquete lleva **todos** los jars y la instalación elige el que le toca a este
servidor. Que dos calcen es un error del paquete y se dice al instalar.

`client` es distinto: son los jars que bajan **los jugadores**, así que se
instalan **todos**, van a `w4ve/client/` y W4VE escribe al lado un
`latest.json` con la versión y el sha256 de cada uno. Sirviendo esa carpeta
(ver `http`), el mod puede comprobar su versión sin que nadie suba nada a mano.

### `config`

```json
{"kind": "config", "file": "config/mipieza.json", "keep_on_update": true}
```

Con `keep_on_update`, el archivo que el operador editó no se pisa nunca: el
default nuevo llega como `.new` al lado, y `doctor` lo menciona.

### `service`: un proceso

```json
{
  "kind": "service",
  "name": "mipieza-hub",
  "command": ["w4ve/service/mipieza/venv/bin/python", "-m", "uvicorn",
              "main:app", "--host", "127.0.0.1", "--port", "8792"],
  "cwd": "w4ve/service/mipieza",
  "restart": "on-failure",
  "python": {"requires": ["fastapi", "uvicorn", "httpx", "pillow"]},
  "health": {"http": "http://127.0.0.1:8792/health", "every": 30},
  "after": "otro-servicio",
  "mcdr": false,
  "stop_grace": 10
}
```

- **`python.requires` se declara, no se instala.** `w4ve doctor` lo comprueba
  **con el intérprete que el servicio va a usar de verdad** (el del venv), así
  que un servicio que iba a morir al arrancar se sabe antes.
- **`health.http`** es readiness de verdad. Si tu proceso habla su propio
  protocolo y no puede contestar una URL, usa `health.tcp` (`"127.0.0.1:30001"`),
  que se pregunta **una sola vez**: un puerto abierto dice "arrancó", no "está
  bien", y preguntarlo cada treinta segundos llena el log del otro proceso.
- **`after`** espera a que el otro esté **arriba de verdad**, no a que exista.
- **`mcdr: true`** pone el paquete de compatibilidad en su `PYTHONPATH`, para un
  proceso que importa `mcdreforged` sin ser un plugin.

### `http`: una ruta pública

```json
{"kind": "http", "name": "mipieza-upload", "path": "/v1/upload", "exact": true,
 "proxy": "127.0.0.1:8792", "max_body": "8m", "why": "subidas desde el juego"},
{"kind": "http", "name": "mipieza-media", "path": "/v1/media/",
 "serve": "w4ve/storage/mipieza/media", "cache": "immutable"}
```

O sirves una carpeta (`serve`) o mandas a un servicio (`proxy`), nunca las dos.
Después:

```sh
w4ve http                       # lo imprime
w4ve http --out /etc/nginx/conf.d/mipieza.conf
w4ve http caddy                 # para Caddy, que saca el certificado solo
nginx -t && systemctl reload nginx
```

Las cuatro cosas que cuestan una noche las escribe W4VE por ti: **el orden**
(las rutas exactas antes que los prefijos), el `client_max_body_size`, la
cabecera de cache propia de cada ruta (sin ella hereda la del vecino y el
navegador se descarga tus imágenes) y la barra final del `alias`.

### `storage`: una carpeta con techo

```json
{"kind": "storage", "name": "mipieza-music", "dir": "w4ve/storage/mipieza/music",
 "quota": "2G", "keep": "30d", "by": "atime", "why": "caché de audio"}
```

- **`by: "atime"`** es lo que quiere una caché: una canción que suena cada día
  se queda aunque sea la más vieja. `mtime` es lo que quiere un registro.
  Confundirlos es tirar justo lo que la gente usa.
- El guardián hace sitio solo cada cinco minutos; `w4ve storage` dice cuánto
  hay y `w4ve storage --purge` lo hace ahora.
- **Quitar la pieza nunca borra los datos.** El `remove` dice dónde quedaron.

### `timer`: algo que se repite

```json
{"kind": "timer", "name": "mipieza-sync", "command": ["python3", "sync.py"],
 "every": "60s", "cwd": "w4ve/service/mipieza", "why": "publicar emotes nuevos"}
```

Lo corre el guardián, no cron. La diferencia: **un timer que falla lo dice**
(una vez, y luego lleva la cuenta, para no enterrar el journal), y cuando vuelve
a funcionar también lo dice.

### `watch`: una línea de la consola

```json
{"kind": "watch", "name": "mipieza-restart", "on": "\\[MiPieza\\] RESTART_REQUESTED",
 "do": "restart", "why": "el mod copió archivos de región y pide volver"}
```

---

## 5. El ciclo completo, tal como lo ve quien instala

```sh
w4ve install ./mi-pieza-1.0.0.w4ve
#   pregunta lo que necesita saber, ahí mismo
w4ve http --out /etc/nginx/conf.d/mi-pieza.conf
nginx -t && systemctl reload nginx
w4ve run
```

Y para cambiar algo después:

```sh
w4ve configure mi-pieza            # sólo lo que falta
w4ve configure mi-pieza --all      # todo, de nuevo
w4ve configure mi-pieza --show     # qué hay puesto (nunca imprime secretos)
w4ve configure mi-pieza --set music=yes    # sin preguntas, para scripts
```

O abriendo el `w4ve.toml` y editándolo a mano, que es para lo que está escrito
con los comentarios dentro.

---

## 6. Lo que W4VE se niega a hacer, y por qué

- **No instala paquetes de pip.** Los declara y los comprueba. Bajar código de
  internet y ejecutarlo es una decisión del operador, no de una herramienta.
- **No borra datos al quitar una pieza.** Dice dónde están.
- **No reinicia tu servidor solo.** Dice lo que queda pendiente y ofrece el
  comando de tu `w4ve.toml`.
- **No arranca un servicio a medio configurar.** Prefiere no arrancar y decir
  qué falta.
- **No escribe un secreto en ningún sitio que no sea `w4ve/secrets.json`**, y
  `doctor` avisa si alguien le abrió los permisos.
