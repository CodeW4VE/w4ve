# Player identity authority (development)

W4VE now provides paired-instance proofs, scoped player sessions and live
resource grants. This is a local control-plane API, not a public login service.
Discord account linking, role synchronization and the installation wizard are
still pending. Minecraft companion support for this new proof is also pending.

`w4ve api token wavechat --scope identity` issues a service token. It permits
only identity session/inspect/authorize routes; it cannot read server logs or
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

Validated 14 October 2026: 19 authority/API/CLI tests on Windows loopback TCP
and from an installed distribution on isolated Linux; 24 existing API tests
including real Unix sockets pass on Linux. No real player pairing or Discord
linking tested. Windows Unix server support is unavailable in socketserver;
the API reports that limitation and can be tested over token-protected loopback.
# Sonda de servicio

`POST /v1/identity/health` (SDK `identity_health()`) exige scope `identity` o
`admin`, incluso cuando el API local tiene tokens desactivados. Devuelve
`ok`, `service: identity`, `schema: 1`, sin credenciales ni datos de jugadores.
Confirma acceso al servicio; no certifica emisores, grants, guardian o Minecraft.
