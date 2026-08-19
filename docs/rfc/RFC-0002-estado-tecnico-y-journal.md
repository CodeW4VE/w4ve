# RFC-0002: el estado técnico, el journal y quién manda

**Estado:** aceptado e implementado, verificado el 19 de agosto de 2026
**Fecha:** 19 de agosto de 2026
**Autoridad:** `W4VE-MANIFIESTO.md` v1.3, tarea 20.6, y el criterio de salida de M1
**Depende de:** [RFC-0001](./RFC-0001-guardian-y-propiedad-del-proceso.md)

RFC-0001 dice quién es el dueño del proceso. Este dice quién es el dueño de la
verdad: qué archivo manda cuando dos se contradicen, qué pasa cuando algo muere
a mitad de una operación, y por qué el estado nunca puede ser lo único que sabe
qué hay instalado.

El criterio que hay que cumplir lo fija el manifiesto en M1, y es exigente:
**matar el proceso durante una operación de estado no puede producir una
instalación aceptada a medias.**

---

## 1. Los tres archivos, y su jerarquía

```text
w4ve.toml                 lo escribís vos. Dice lo que el servidor debe tener.
w4ve/state.json           generado.  Dice lo que el servidor tiene.
w4ve/run/                 volátil.   Dice lo que está pasando ahora mismo.
```

La regla es la de siempre y no cambia: **si los dos primeros se contradicen,
`w4ve sync` hace que el segundo obedezca al primero.** Lo que este RFC añade es
el tercer nivel y las garantías de escritura de los otros dos.

**`w4ve.toml` es de la persona.** Se edita a mano, lleva comentarios y nadie se
los come: cuando un comando escribe ahí, hace cirugía de texto sobre las líneas
que toca en vez de volcar un TOML reconstruido. Los bloques que instala una
pieza (`[services.<name>]`, `[watch.<name>]`) llevan un comentario diciendo
quién los puso, para que se puedan leer, comentar y cambiar sin adivinar de
dónde salieron. Es el principio que sacamos de Fallen: el estado que un
operador necesita tocar vive en archivos planos, visibles, no escondido en una
base de datos.

**`w4ve/state.json` es una caché con memoria de lo hecho.** Guarda, por pieza:
la versión, de dónde salió (catálogo o paquete), qué archivos escribió con su
hash, cuáles de esos archivos son editables por el operador, qué servicios y
qué watches declaró, y si queda un reinicio pendiente. Se puede borrar sin
perder el servidor: `w4ve adopt` lo reconstruye leyendo lo que hay en disco.
Borrarlo cuesta, eso sí, la memoria de los hashes, o sea la capacidad de
distinguir un archivo que cambiaste vos de uno que cambió solo.

**`w4ve/run/` no sobrevive a nada y no debe.** Ahí viven el lock, el estado del
guardián, el journal de transiciones, las consolas y el journal de instalación.
Todo lo de esa carpeta es reconstruible o desechable por definición.

## 2. Ninguna escritura a medias

Toda escritura de estado sigue el mismo patrón, y no hay excepciones:

1. escribir en un nombre temporal al lado del destino,
2. `flush` y `fsync`, que es lo que empuja los bytes al disco de verdad,
3. `os.replace` sobre el destino, que es la única operación que el sistema de
   archivos promete atómica.

Antes de reemplazarlo, el archivo que se va se copia a `state.json.bak`. Al
leer, si el actual no es JSON válido, se lee esa copia y se dice en voz alta
cuál se está usando. Se pierde el último cambio y **no** se pierde el registro
del servidor, que es el intercambio correcto: un `state.json` vacío hace que
W4VE olvide lo que instaló, y eso es peor que olvidar una línea.

## 3. El journal de instalación

Instalar un paquete toca varios archivos y dos secciones del `w4ve.toml`.
Deshacerlo en memoria solo sirve mientras el proceso vive; un `SIGKILL`, un
contenedor que se cae o un corte de luz dejan medio paquete en disco y nada que
lo sepa.

Por eso, antes de cada paso, se escribe una línea en
`w4ve/run/install.journal`, con `fsync`, diciendo lo que va a pasar:

```json
{"op": "begin", "piece": "wavechat-media", "version": "1.0.2"}
{"op": "write", "path": ".../mods/shapeboard-1.7.0+1.21.jar", "backup": null}
{"op": "write", "path": ".../config.json", "backup": ".../config.json.w4ve-old"}
{"op": "service", "name": "wavechat-media"}
```

El journal se escribe **antes** de la acción, no después: uno que registra lo
ya hecho no sirve de nada justo en el caso para el que existe. Al terminar bien,
el journal se borra; ese borrado **es** el commit.

Cualquier comando que vaya a tocar el servidor comprueba primero si hay un
journal. Si lo hay, algo murió a mitad: se recorre al revés, se borra lo que se
escribió, se devuelven a su sitio los archivos que se habían apartado, se
quitan los servicios y watches que se hubieran declarado, y se saca la pieza
del estado. Lo que no se pueda deshacer se nombra, uno por uno, en vez de
tragárselo.

`w4ve repair` hace lo mismo a propósito y `w4ve doctor` lo reporta como
problema, así que el estado a medias es visible de tres formas distintas y no
hay que tropezarse con él.

## 4. La propiedad de los archivos

Una pieza es dueña de los archivos que escribió, y de ninguno más.

- Se guarda el **hash** de cada archivo al instalarlo. `remove` borra solo los
  que siguen coincidiendo; el que cambió se deja donde está y se dice, porque
  alguien quiso ese cambio y un comando de desinstalar no es permiso para tirar
  trabajo ajeno.
- Un componente `config` (o cualquiera marcado `keep_on_update`) se anota como
  **editable**: al actualizar no se pisa, el default nuevo llega como `.new` al
  lado, y `doctor` no lo reporta como cambiado, porque editarlo es su función.
- Un archivo que la pieza escribió y que ya no está se reporta como problema:
  falta algo que debería estar.
- Los archivos que nadie instaló (`w4ve adopt` los ve y no los toca) se cuentan
  y se dejan en paz.

## 5. Dónde aterriza cada cosa

Los archivos de una pieza van al directorio del juego, salvo lo que cuelga de
`w4ve/`, que va a la raíz del servidor. La razón es concreta: bajo MCDR el
juego vive en `server/` y la raíz es otra cosa, así que un servicio instalado
en `game_dir` quedaría en un sitio distinto según el layout, y la pieza tendría
que saber cuál. Con esta regla, una pieza con servicio se instala igual en un
servidor suelto y en uno con layout de MCDR.

## 6. Lo que este RFC no hace todavía

- **No hay lockfile de resolución.** El `w4ve.toml` fija versiones por pieza y
  el catálogo resuelve el resto en cada comando. Un lockfile con el grafo
  entero resuelto es de M6, junto al solver.
- **No hay transacción entre varias piezas.** El journal cubre un paquete; una
  instalación de cinco piezas que muere en la tercera deja las dos primeras
  instaladas y la tercera revertida. Es honesto y es visible, pero no es
  atómico de punta a punta.
- **No hay migraciones de esquema.** El estado declara `schema: 1`; cuando
  cambie hará falta una migración escrita, no un `try/except`.

## 7. Criterio de aceptación

Verificado el 19 de agosto de 2026, con pruebas que matan procesos de verdad:

1. ✅ Un `SIGKILL` en mitad de una instalación deja el journal en disco, el
   siguiente comando lo encuentra, borra el jar a medio poner y deja la carpeta
   como estaba.
2. ✅ Un archivo que la instalación había apartado vuelve a su sitio con su
   contenido anterior.
3. ✅ Un `state.json` roto a mano se lee desde la copia anterior, diciendo que
   lo hace, en vez de empezar de cero.
4. ✅ Una escritura normal no deja archivos temporales ni versiones a medias.
5. ✅ `remove` no borra un archivo que cambió desde que lo escribimos, y lo
   dice.
6. ✅ `doctor` reporta una instalación interrumpida como problema, y `w4ve
   repair` la deshace a propósito.
