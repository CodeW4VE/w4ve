# Player identity authority (development)

W4VE now provides paired-instance proofs, scoped player sessions and live
resource grants. This is a local control-plane API, not a public login service.
Guided local pairing and the WaveChat companion/client proof handshake exist
in development. Discord account linking, role synchronization, remote enrollment,
the advanced policy wizard and real Minecraft/GL validation are still pending.

`w4ve api token wavechat --scope identity` issues a service token. It permits
only identity routes (including health and typed instance sessions); it cannot read server logs or
control processes/jobs/plugins. `admin` can pair instances, change grants and
block accounts. Every identity route requires a token, including on Unix sockets
configured to allow tokenless access for other operations.

Routes (all POST):

| Route | Scope | Input |
|---|---|---|
| /v1/identity/instances | admin | community, instance, optional secret/enabled |
| /v1/identity/grants | admin | community, audience, resource, subject, operations list |
| /v1/identity/block | admin | community, subject, blocked boolean |
| /v1/identity/session | identity or admin | proof, audience |
| /v1/identity/inspect | identity or admin | session, audience |
| /v1/identity/authorize | identity or admin | session, audience, resource, operation |

Pairing returns a generated secret once to the authenticated administrator.
Re-pairing/rotation invalidates issued sessions. Never put pairing secrets in
client configs, public manifests or logs. The authority database belongs under
the private W4VE identity directory (mode700/database600 on Linux). Issuer
secrets are sensitive; session bearer tokens are stored as SHA256 digests.

Proof wire format:

```
w4ve1.<base64url UTF8 JSON without padding>.<base64url HMAC-SHA256 without padding>
```

MAC covers ASCII `w4ve1.` plus the exact encoded body, using the paired issuer's
UTF8 secret. Fields: community, instance, audience, uuid, name, issued, expires,
nonce. UUID is canonicalized; Minecraft name must match 1..16 ASCII letters,
digits/underscore. Issued/expires are integer Unix seconds; boolean/NaN rejected.
Proof lifetime max300s, future issued skew max30s, expires must be in the future.
Nonce is single-use per community/instance, persisted until expiry. A new proof
is needed if a redemption response is lost. Input max2048 characters.

The issuer is trusted to attest a connected account. A signature is not itself
Mojang authentication; online-mode/pairing and trustworthy companion/adapters
must supply that guarantee. An IGN from an editable profile is not such proof.

Sessions last900s, are bound to audience and issuer, and expose canonical
`minecraft:<UUIDhex>` subject, verified presentation name/origin/community,
expiry and policy_revision. API `inspect` never returns an issuer secret or
session token. `authorize` requires an explicit exact-resource/operation grant;
`*` subject means any valid admitted principal in that community, not anonymous.
Grants are separate from account/session identity. Membership blocking invalidates
existing sessions and refuses new proofs until unblocked.

Changing grants, issuer pairing or blocking updates that community's revision.
Consumers must reauthorize operations and reset cached views when it changes.
Other communities do not inherit a grant. API returns generic denial/invalid
request responses rather than echoing supplied credentials.

The public client SDK provides identity_session, identity_inspect and
identity_authorize. WaveChat uses that SDK over local API, rather than reading
private authority files. Services must fail closed if W4VE is unavailable.

Validated 14 October 2026: 20 authority/API/CLI tests on Windows loopback TCP
and from an installed distribution on isolated Linux; 24 existing API tests
including real Unix sockets pass on Linux. No real player pairing or Discord
linking tested. Windows Unix server support is unavailable in socketserver;
the API reports that limitation and can be tested over token-protected loopback.

# Sonda de servicio

`POST /v1/identity/health` (SDK `identity_health()`) exige scope `identity` o
`admin`, incluso cuando el API local tiene tokens desactivados. Devuelve
`ok`, `service: identity`, `schema: 1`, sin credenciales ni datos de jugadores.
También devuelve `capabilities`, objeto de versiones de protocolo: player_proof,
instance_sessions, connection_context, resource_recipients y policy_revision,
todas con versión entera 1. Los consumidores pueden exigir esas garantías sin
deducirlas de la versión de desarrollo del producto. Una capacidad futura se
añade sin quitar las versiones compatibles; un booleano no es una versión.
Confirma acceso al servicio; no certifica emisores, grants, guardian o Minecraft.

WaveChat exige estas capacidades antes de cada operación del SDK de identidad,
sin caché positiva que sobreviva a un cambio de runtime. Una inspección debe
incluir connection incluso para una sesión legacy (cadena vacía); omitir el
campo ya no permite rebajar silenciosamente la protección de reconexiones.
La sonda /ready de WaveChat devuelve 503 ante incompatibilidad o dependencia
caída; /health permanece 200 como diagnóstico. El manifest del servicio usa
/ready para que el guardian represente disponibilidad y no solo proceso vivo.

## Managed service credential

A service component may declare `w4ve_api: identity`. The installed guardian
supplies its SDK through PYTHONPATH, an absolute W4VE_SERVER_ROOT, the configured
API address and W4VE_IDENTITY_TOKEN. The credential stays out of TOML and process
commands. Other operator scopes cannot be requested through this declaration.
W4VE_GAME_DIR is the absolute Minecraft working directory, taken from the
registered game process or, if absent, server.game_dir in the guardian config.
Managed metadata overrides conflicting piece environment defaults. Consumers
can resolve game files without reading the guardian's private state or guessing
the MCDR layout. Explicit user-selected paths remain the consumer's choice.
The API starts before services and requires tokens on all protected routes when
identity services are present, including Unix sockets. Existing anonymous local
API consumers then need their own credentials. An explicitly disabled API is
respected; only the dependent service is rejected during CLI setup.

Tokens.service_identity reuses one identity token named service:<service>, with
cross-process writer locking. Ambiguous names, a different scope or corrupt
registry fail without overwriting existing data. Removing a service revokes its
token. Manual revocation denies the running process; a later restart creates a
new credential if that service is still declared. This is API capability control,
not an operating-system sandbox for services sharing the guardian's user.

## Paired instance transport role (14 October 2026)

Player proofs may include a canonical lowercase UUID-hex `connection` context.
The paired issuer signs it alongside the account; it persists in the player
session and inspection response. Legacy sessions/proofs use an empty context.
The additive database upgrade preserves them. It is a connection correlation
claim from the trusted issuer, not an independent online-presence check, a new
account or an extra permission. Never accept a context from unsigned player JSON.

`w4ve-instance1.<base64url JSON>.<HMAC-SHA256>` uses the paired issuer secret,
but has exactly community/instance/audience/issued/expires/nonce claims and no
player UUID or display name. It cannot be exchanged as `w4ve1` player proof.
Instance sessions last 300 seconds, are stored hashed in a separate table,
and are invalidated by issuer rotation/disable. Proof replay is persisted.

Identity-scoped SDK methods are `identity_instance_session(proof, audience)`,
`identity_instance_inspect(session, audience)` and
`identity_instance_recipients(session, audience, resource, operation, players)`.
The latter accepts at most 100 canonical lowercase UUID hex strings from the
trusted paired instance's connected roster. A recipient needs both explicit
instance/resource sharing and current member operation permission; blocked
members are excluded. An instance session never becomes a player subject.

`POST /v1/identity/instance-resources` is admin-only and replaces that instance's
resource list (empty revokes all). Guided pairing initializes sharing once.
Old local bindings are upgraded only by an explicit configure operation;
later revocations survive repeated configure. This does not implement remote
instance enrollment, channel administration UI or Discord account linking.
