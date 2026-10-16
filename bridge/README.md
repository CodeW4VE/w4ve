# Adaptador Fabric de W4VE

Desarrollo privado para Minecraft 1.21 y Java 21. No publicado ni instalado en
producción. El runtime entrega `W4VE_GAME_SOURCE_CONFIG`; sin ese descriptor,
la captura permanece apagada. El adaptador no necesita WaveChat ni Discord.

Captura callbacks tipados de arranque/parada, entrada/salida y chat público
normal después de los filtros Fabric. Chat requiere `[bridge] chat=true` en
W4VE. Excluye `/msg`, `/say`, `/me`, tipos desconocidos, mensajes de sistema y
mensajes con filtro de contenido. Conserva texto decorado y hash de la firma,
cuando existe; ese hash no verifica por sí solo la autenticidad de Mojang.

`ConnectionContexts` comparte la identidad de la conexión con consumidores
opcionales. `joined(Object)` es idempotente para el mismo objeto; `respawn`
transfiere el contexto cuando Minecraft sustituye el jugador sin reconectar.
Una conexión nueva recibe otro contexto. WaveChat usa esta API opcional y
rechaza una API presente pero incompatible en vez de inventar otra identidad.

El hilo del juego solo encola, sin esperar HTTP ni disco. Cola de 256 elementos,
spool privado de 4096 eventos/8 MiB, máximo 1000 contextos y plazos de IPC.
Rechazo permanente, presión o pérdida de continuidad exigen reconciliación.
La frontera durable es el fsync del spool, no el callback del juego. Un cierre
forzado antes de ese fsync puede perder capturas; no se promete entrega ante
fallo eléctrico ni recuperación automática de una fuente que perdió eventos.

Compilar desde `w4ve`, con Gradle 9.5.1 y Java 21:

```sh
JAVA_HOME=/ruta/jdk-21 ../WaveMotes/gradlew -p bridge build
```

Pruebas Java puras:

```sh
python tools/check_bridge_contexts.py --java-home /ruta/jdk-21
python tools/check_bridge_java.py --java-home /ruta/jdk-21 --gson /ruta/gson.jar
python tools/check_bridge_spool.py --java-home /ruta/jdk-21 --gson /ruta/gson.jar
```

Prueba del runtime instalado: `tools/check_bridge_installed.py`. Prueba de
Minecraft dedicado, clientes TCP, prueba de identidad firmada, respawn,
reconexión, exclusión de privados/comandos y parada:
`tools/run_bridge_game_qa.py --help`. Requiere Podman, imágenes cacheadas,
Java exportado y dependencias Node con lockfile. Usar también `--chat-off`.
El driver crea solo carpetas nuevas `/tmp/w4ve-bridge-qa-*`; el juego está en
red `none` y los clientes comparten únicamente ese namespace.

Todavía no distribuye automáticamente el jar ni implementa entrega WaveChat a
jugadores. El16 se añadieron grants de lectura separados y autorización
histórica en el runtime; el hub instalado ya consumió cuatro chats de Fabric
real en QA, sin eco. No confundir ese recorrido de entrada con un relay de
salida o producto completo. Evidencia y continuación:
`../../WaveChat-Server/docs/FABRIC-HUB-REAL-2026-10-16.md`.
