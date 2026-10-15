# RFC-0004: eventos tipados y registro acotado del juego

Estado14/10/2026: núcleo, API local y lifecycle de guardian implementados y
probados con productor simulado. Adapter Fabric real, spool JVM y consumidores
de servicio todavía en implementación. No afirmar chat Minecraft real entregado.
Autoridad: W4VE-MANIFIESTO5.7; contrato W4VE/WaveChat y petición del usuario de
absorber complejidad en la plataforma y conservar las funciones de WaveChat.

## Contrato implementado

gameevents.py conserva un SQLite privado de eventos tipados por instancia local.
No interpreta stdout, no conoce canales Discord ni permisos de WaveChat, y no
convierte un evento antiguo en una sesión de jugador conectado. El guardian
crea la fuente antes de arrancar su proceso server y liga después PID/starttime.
Cada arranque recibe boot aleatorio y credencial distinta; solo su digest queda
en SQLite. La credencial privada no es token de operador, de servicio identity,
de instancia remota ni de jugador.

Activación explícita en TOML:

```toml
[bridge]
enabled = true
chat = false # true es opt-in del registro de chat
retention = 300
max_events = 4096
max_bytes = 8388608
```

Tipos/valores o claves desconocidas fallan sin habilitar la fuente. El API local
se activa por defecto si se solicita bridge; un api.enabled=false explícito se
respeta y deja la fuente sin iniciar. Config inválida o descriptor ajeno no
impiden Minecraft: el guardian informa fallo de bridge y preserva el archivo.
Con bridge activo las rutas protegidas exigen token incluso sobre Unix. No se
puede aprovechar que el socket sea local para usar una credencial de productor
como operador. El fallback de consola de W4VE se conserva.

El hijo recibe solo W4VE_GAME_SOURCE_CONFIG, ruta absoluta del descriptor privado
w4ve/run/game-source.json. Incluye boot, token, endpoint Unix/loopback y vínculo
PID/starttime; archivo600, reemplazo atómico y fsync. No pasar secretos en argv.
Registro y descriptor son archivos privados del runtime, no API de consumo del
hub. El companion es el lector previsto del descriptor de arranque.

## Ingreso y consulta

POST /v1/game/source/status y /append usan exclusivamente la credencial de fuente,
autenticada antes de leer el JSON. Ni admin ni identity pueden fabricarla por
presentar sus propios tokens. El API no ofrece open_source remoto. SDK:
game_source_status y game_source_append. Ingreso solo de peers locales, aun si
el operador publica explícitamente el API general. Unix sigue siendo preferido.

Body máximo16KiB; Content-Length acotado, sin Transfer-Encoding, timeout3s y
cuatro operaciones autenticadas concurrentes como máximo. 429 indica reintentar
el mismo evento. Esto no es un sandbox contra el dueño del proceso ni sustituye
las protecciones del socket; el servidor HTTP general sigue teniendo su modelo
de conexiones previo. No declarar rate limiting completo por host ya entregado.

Envelope exacto: schema1 entero, boot32hex, id32hex, sequence positiva, kind,
occurred (segundos UTC) y data. Secuencia ordenada por boot. Tipos actuales:

| kind | data |
| --- | --- |
| server.started | objeto vacío, primero y solo una vez |
| player.joined | UUID32hex, nombre de cuenta, connection32hex |
| player.left | UUID32hex, connection32hex |
| player.chat | UUID32hex, connection32hex, texto1..256, signature_sha256 o vacío |
| server.stopping | objeto vacío, admite después solo leave/stopping |

El nombre canónico del chat se deriva del join previo; el productor no lo
sobrescribe en el payload chat. Otro contexto del mismo UUID no puede hablar con
el anterior; un leave atrasado no quita el nuevo. El hash de firma es una
correlación declarada por el companion confiable, no verificación criptográfica
independiente de Mojang. No vincular mensajes por coincidencia de texto/nombre.

Mismo boot/id/secuencia/payload retorna el cursor previo, sin reaplicar roster.
Contenido cambiado, huecos o IDs reutilizados:409 y reconciliación explícita.
Consulta source/status informa el último sequence confirmado. Al vencer recibos,
no resucitar contenido ni asignar otra identidad al reintento.

POST /v1/game/events/poll es por ahora admin-only y siempre autenticado. SDK
game_events(cursor,epoch,limit,chat). Un servicio identity ordinario no puede
leer este registro, ni siquiera sin chat. NO entregar admin a WaveChat para
sortearlo: falta habilitar consumidores de servicio con grants separados.

Poll limita escaneo100, avanza también sobre chat oculto, tiene epoch/cursor,
floor de retención y reset explícito. Chat no se devuelve salvo opt-in de consulta
y política chat=true. Este reset obliga al consumidor a reconciliar su estado;
no promete retención eterna ni entrega exactly-once fuera del plazo.

## Persistencia, presión y recovery

Misma transacción: contenido, cursor, secuencia y estado de conexiones. Cuota
llena devuelve presión y revierte el avance; no elimina silenciosamente un
evento nuevo para que una prueba pase. Retención5..3600s,4096eventos/8MiB por
defecto. Límite64fuentes y1000jugadores por boot. Son cuotas de payload lógico;
SQLite/WAL tiene overhead y límite adicional de páginas. La retención es lógica,
no una promesa de borrado forense de páginas, WAL o backups. Desactivar chat
impide nuevos ingresos/lecturas; registros antes consentidos vencen por TTL.

Received es monotónico lógico para que un ajuste del reloj no cree huecos de
retención delante de eventos aún conservados. Occurred tiene ventana temporal
acotada; un reloj que salte fuera de ella exige diagnóstico/reconciliación.

SourceRuntime serializa descriptor entre threads y guardianes con bloqueo del
SO. Handover preserva boot/credencial si coincide PID/starttime y sigue vivo;
actualiza el endpoint tras abrir el nuevo API. No adoptar por PID solo ni abrir
otra fuente para un proceso heredado. Heartbeat local comprueba el proceso cada
2s y guarda last_alive; recuperación de un proceso ya muerto no renueva el plazo
de captura con la hora en que el nuevo guardian lo encontró.

Fuente retirada admite dentro de retención eventos ya capturados antes de su
cutoff, pero su roster público queda vacío. Puede reconstruir historial, nunca
presencia nueva. El cierre de W4VE conserva API hasta terminar los hijos para
permitir que el companion vacíe stopping/leave; luego reconcilia y cierra API.

## Validación y continuación

Windows:28 pruebas pasan y2 de guardian se saltan por requerir Linux.
Linux aislado con runtime instalado:30 gameevents PASS, incluyendo dos guardianes
reales, credencial recibida por hijo simulado, flush durante shutdown y handover
sin cambiar PID/boot. Logs en D:/MineWave/W4VE/notas/ecosistema/
qa-portabilidad-2026-10-13/identity-2026-10-14/game-events-*.txt.
Se ejecutan además identity/API/package/pairing/settings/runtime/handover para
detectar regresiones; conservar sus resultados completos por ejecución.

Falta el adapter Fabric propio de W4VE: callbacks de chat público/join/leave y
lifecycle reales, cola acotada, spool durable privado y negociación local. La
identidad WaveChat y el source deben compartir el MISMO connection del objeto
ServerPlayer: dos UUID aleatorios independientes romperían los filtros por
reconexión. Nunca re-mintar un proof de conexión actual para vaciar spool viejo.

Después: grants de lectura de eventos para el servicio y autorización de actor
histórico en W4VE; ingreso canónico a WaveChat con nonce/event ID; entrega dirigida
con journal/reconciliación y comprobación de conexión justo antes de mostrar.
No usar tellraw@a ni confundir el nuevo registro con un relay ya registrado.
