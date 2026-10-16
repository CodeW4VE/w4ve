# Lectores de eventos para servicios

16 de octubre de 2026. Complementa RFC-0004: el poll deja de ser exclusivamente
de operador. La captura Fabric y el spool probados el 15 siguen siendo la fuente;
este cambio autoriza lectores, todavía no entrega mensajes al hub ni al juego.

## Declaración y credenciales

Un componente `service` de una pieza o un bloque `[services.nombre]` puede
declarar `w4ve_events = "presence"` o `w4ve_events = "chat"`. Presence permite
lifecycle y joins/leaves, que contienen UUID, nombre y contexto de conexión.
Chat permite además solicitar el texto público capturado. Es acceso al registro
local de este runtime, no un permiso sobre canales de WaveChat ni sobre otra
comunidad remota. Una pieza debe explicar esta audiencia antes de pedirlo.

El guardian inyecta `W4VE_EVENTS_TOKEN`, `W4VE_EVENTS_ADDRESS`,
`W4VE_SERVER_ROOT` y el SDK en PYTHONPATH. La credencial está separada de
`W4VE_IDENTITY_TOKEN`; no concede identidad, comandos, lectura del estado del
servidor ni escritura de capturas. Un servicio puede solicitar ambos permisos
de manera independiente. Una API explícitamente deshabilitada no se activa
silenciosamente para satisfacer la declaración.

El SDK público `W4VE(..., token=events_token).game_events(cursor, epoch=...,
limit=100, chat=False)` consulta POST `/v1/game/events/poll`. Los scopes
`game-events` y `game-chat` están separados de la jerarquía read/write/admin.
Admin conserva su inspección; identity/read/write no leen el registro.
Game-chat incluye game-events, pero game-events no incluye texto de chat.

Chat exige simultáneamente `[bridge] chat=true`, declaración chat para el
servicio y `chat=True` en la consulta. Sin chat, el cursor avanza también sobre
las filas ocultas para que el lector no quede atascado. Retención, límites,
epoch/floor/reset y presión del registro permanecen vigentes. El consumidor
debe reconciliar tras reset; una captura no prueba presencia actual.

## Cambios y revocación

Se conserva una credencial por servicio y política bajo el nombre privado
`service-events:<nombre>`. Cambiar de política rota la credencial; no convierte
la anterior en un permiso de otra clase. El CLI revoca al retirar el servicio
y al cambiar la declaración mediante set_service. Los demás servicios y sus
credenciales de identidad no se revocan como efecto colateral.

Cada petición de un lector gestionado comprueba además el w4ve.toml actual.
Quitar el bloque, cambiar presence/chat o romper/quitar la configuración corta
su acceso inmediatamente, sin caché de autorización. Restaurar exactamente la
declaración puede volver a habilitar una credencial que aún exista; para una
revocación permanente se debe retirar/revocar el token. La rotación se realiza
al aprovisionar con la nueva declaración. Un token emitido explícitamente por
el operador es un grant directo del registro y no depende de un servicio.

El snapshot público del guardian y la lista de tokens no publican las claves.
Los servicios ordinarios sin estos permisos no dependen de que el registro de
tokens sea legible para arrancar. Esto es separación de credenciales/API entre
procesos cooperativos: no promete aislamiento frente a código hostil que tenga
el mismo usuario y acceso completo al directorio privado del runtime.

## Validación y límites

`tests/test_gameevents_services.py` prueba HTTP real, contenido oculto,
combinación de opt-ins, rechazo de control/identity/source, rotación, revocación,
ediciones manuales, configuración inválida y bootstrap del guardian. El test de
paquete verifica que el permiso declarado llega al TOML instalado.

La evidencia completa y las limitaciones de cada ejecución están en
`WaveChat-Server/docs/LECTORES-DE-EVENTOS-2026-10-16.md` y en
`notas/ecosistema/qa-portabilidad-2026-10-16/event-grants`.

Siguiente: vincular cada boot a su identidad de origen antes de capturar,
autorizar por ID una captura histórica con permisos actuales sin fabricar una
sesión de jugador, consumidor canónico del hub con cursor/epoch/idempotencia,
y relay dirigido a la conexión vigente. No dar admin al hub como atajo.
