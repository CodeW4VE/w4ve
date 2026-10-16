# Origen de una captura y autorización histórica

16 de octubre de 2026. Esta API resuelve historial confiable; no emite una
sesión de jugador ni afirma que siga conectado.

Al preparar un boot nuevo, SourceRuntime toma un snapshot privado de los
bindings habilitados de la autoridad local: owner, comunidad, instancia,
audience y epoch de la autoridad. No copia secretos. Se guarda en la fila
del source antes de arrancar el juego; handover conserva la fila original.
Los boots existentes al migrar la base reciben `{}`, sin backfill. Si no había
autoridad/pairing al arrancar, configurar uno más tarde no atribuye ese historial.

POST `/v1/game/events/authorize-chat` requiere game-chat, el grant vigente del
servicio y captura chat habilitada. Campos exactos del cuerpo:
`epoch`, `boot`, `id`, `owner`, `resource`. El SDK expone
`game_event_authorize_chat(epoch, boot, event_id, owner, resource)`.

W4VE consulta la captura por esos IDs en su registro. El lector no suministra
UUID, contexto, nombre ni texto. Un evento expirado, de otro epoch, desconocido
o distinto de player.chat se rechaza. Se requiere el binding conservado en el
boot, el mismo epoch de autoridad y el binding/issuer todavía habilitado.
Se revalidan canal compartido, bloqueo y grants actuales de chat.read y
chat.publish. Allowed false no autoriza ningún efecto en el consumidor.

La respuesta incluye kind=captured-chat, actor canónico, resource,
policy_revision, allowed y el evento almacenado con su nombre derivado del join.
No contiene token, secreto ni sesión. Que el jugador haya salido o el source
se haya retirado no impide autorizar una captura aún retenida; tampoco restaura
su presencia. La rotación del secreto de proofs no vuelve a firmar el pasado:
el productor del registro y su boot son una frontera de confianza diferente.

Estos grants permiten leer el registro local y resolver bindings locales.
No son un sandbox frente a otro proceso con el mismo usuario y acceso privado.
La autorización comprueba permisos al consumir; no promete reconstruir la
política de canales que existía en el instante de captura.

Pruebas: test_gameevents_bindings.py y test_gameevents_capture_api.py cubren
migración, handover, no atribución tardía, expiración/epoch, desconexión,
revocaciones, campos falsificados y ausencia de sesiones creadas. El consumidor
del hub y el relay a conexiones vigentes son trabajo posterior separado.
