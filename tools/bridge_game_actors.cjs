// TCP clients for the network-none disposable fixture, never a public server.
const mineflayer = require('/qa/node/node_modules/mineflayer')
const { once } = require('node:events')
const fs = require('node:fs')
const crypto = require('node:crypto')
const https = require('node:https')
// Generated only inside the disposable QA fixture. Never a production issuer.
const fixturePath = '/qa/work/fixture-identity.json'
const fixture = fs.existsSync(fixturePath) ? JSON.parse(fs.readFileSync(fixturePath, 'utf8')) : {
  secret: 'QA_ONLY_NOT_A_REAL_SIGNING_SECRET_20261015', community: 'bridge-qa', instance: 'fabric-qa'
}
const bots = new Set()
const proofs = []
const announcements = new WeakMap()
const controls = new Set()
const relayCounts = new WeakMap()
const filteredCounts = new WeakMap()
const readyTimers = new WeakMap()
const nativeMessages = []
function observeRelayText (bot, text) {
  if (text === '[general] BridgeQA_A: QA_NATIVE_RELAY') relayCounts.set(bot, (relayCounts.get(bot) || 0) + 1)
  if (text === '[general] RemoteQA: QA_NATIVE_FILTERED') filteredCounts.set(bot, (filteredCounts.get(bot) || 0) + 1)
}
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
const timer = setTimeout(() => fail(), 90000)
let stage = 'joining'
function fail () {
  fs.writeFileSync('/qa/work/actors-done.json', JSON.stringify({ ok: false, stage }))
  for (const bot of bots) bot.end()
  process.exit(1)
}
async function join (username) {
  // Recipe/inventory semantics are outside this chat fixture. The current
  // 1.21 recipe decoder fails on the vanilla recipe frame; consume that single
  // framed packet as bytes, preserving normal parsing for every chat/login frame.
  const customPackets = { '1.21': { play: { toClient: { types: { packet_declare_recipes: 'restBuffer' } } } } }
  const bot = mineflayer.createBot({ host: '127.0.0.1', port: 25565, version: '1.21', username, auth: 'offline', customPackets })
  bots.add(bot)
  bot.on('message', message => {
    const text = message.toString()
    observeRelayText(bot, text)
    for (const marker of ['QA_PRIVATE', 'QA_COMMAND_ME', 'QA_COMMAND_SAY']) {
      if (text.includes(marker)) controls.add(marker)
    }
  })
  bot.on('error', fail)
  bot.on('kicked', fail)
  await once(bot, 'spawn')
  bot.physicsEnabled = false
  bot._client.registerChannel('wavechat:identity_grant', 'string', true)
  bot._client.on('wavechat:identity_announcement', value => {
    try {
      const announcement = JSON.parse(value)
      if (announcement.protocol !== 2 || announcement.community !== fixture.community || announcement.instance !== fixture.instance) throw new Error('wrong fixture announcement')
      announcements.set(bot, (announcements.get(bot) || 0) + 1)
    } catch (_) { fail() }
  })
  bot._client.registerChannel('wavechat:identity_announcement', 'string', true)
  proofs.push(await proofFor(bot, username))
  return bot
}
async function proofFor (bot, username, full = false) {
  await sleep(300)
  const received = once(bot._client, 'wavechat:identity_grant')
  bot._client.write('custom_payload', { channel: 'wavechat:identity_request', data: Buffer.alloc(0) })
  const [proof] = await received
  const [prefix, body, signature] = proof.split('.')
  const expected = crypto.createHmac('sha256', fixture.secret).update(prefix + '.' + body).digest('base64url')
  if (prefix !== 'w4ve1' || signature !== expected) throw new Error('invalid fixture proof')
  const actor = JSON.parse(Buffer.from(body, 'base64url').toString('utf8'))
  if (actor.name !== username || actor.community !== fixture.community || actor.instance !== fixture.instance || !/^[a-f0-9]{32}$/.test(actor.connection)) throw new Error('missing fixture context')
  return full ? proof : actor.connection // Never persist the full capability.
}
function post (path, body, token = '') {
  return new Promise((resolve, reject) => {
    const request = https.request({ hostname: 'localhost', port: 8792, path: '/v2/chat/' + path,
      method: 'POST', headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: 'Bearer ' + token } : {}) } }, response => {
      let text = ''
      response.on('data', chunk => { text += chunk; if (text.length > 65536) response.destroy(new Error('QA response too large')) })
      response.on('end', () => {
        if (response.statusCode !== 200) return reject(new Error('QA HTTPS request refused'))
        try { resolve(JSON.parse(text)) } catch (error) { reject(error) }
      })
      response.on('error', reject)
    })
    request.setTimeout(5000, () => request.destroy(new Error('QA request deadline')))
    request.on('error', reject)
    request.end(JSON.stringify(body))
  })
}
async function quit (bot) {
  clearInterval(readyTimers.get(bot))
  const ended = once(bot, 'end')
  bot.quit()
  await ended
  bots.delete(bot)
  await sleep(400)
}
async function run () {
  let alice = await join('BridgeQA_A')
  const bob = await join('BridgeQA_B')
  if (process.env.QA_REQUIRE_NATIVE_PAYLOAD === 'true') {
    // Protocol actor, not the graphical client. Alice advertises native support;
    // Bob deliberately remains vanilla to exercise both dispatch branches.
    alice._client.registerChannel('wavechat:native_delivery', 'string', true)
    alice._client.on('wavechat:native_delivery', text => {
      try {
        const envelope = JSON.parse(text), message = envelope.message
        if (envelope.protocol !== 2 || envelope.instance !== fixture.instance || envelope.connection !== proofs[0]
          || message.community !== fixture.community || message.channel !== 'general' || !/^m_[a-f0-9]{32}$/.test(message.id)
          || message.revision !== 1 || message.provenance.kind !== 'native') throw new Error('native delivery scope mismatch')
        nativeMessages.push({ id: message.id, connection: envelope.connection, revision: message.revision })
        observeRelayText(alice, '[' + message.channel + '] ' + message.name + ': ' + message.body)
      } catch (_) { fail() }
    })
    const heartbeat = () => alice._client.write('custom_payload', { channel: 'wavechat:native_ready', data: Buffer.concat([Buffer.from([32]), Buffer.from(proofs[0])]) })
    heartbeat(); readyTimers.set(alice, setInterval(heartbeat, 1500))
  }
  alice.chat('QA_PUBLIC_FIRST')
  await sleep(500)
  bob.chat('QA_PUBLIC_BOB')
  await sleep(500)
  if (process.env.QA_REQUIRE_RELAY === 'true') {
    stage = 'HTTPS-public-relay'
    await sleep(2200)
    const proof = await proofFor(alice, 'BridgeQA_A', true)
    const session = await post('session', { proof })
    await post('messages', { channel: 'general', body: 'QA_NATIVE_RELAY', nonce: 'qa-real-relay' }, session.token)
    const deadline = Date.now() + 10000
    while ((relayCounts.get(alice) !== 1 || relayCounts.get(bob) !== 1) && Date.now() < deadline) await sleep(100)
    if (relayCounts.get(alice) !== 1 || relayCounts.get(bob) !== 1) throw new Error('native relay missing or duplicated')
    await sleep(1500)
    if (relayCounts.get(alice) !== 1 || relayCounts.get(bob) !== 1) throw new Error('native relay repeated')
    const bobProof = await proofFor(bob, 'BridgeQA_B', true)
    const bobSession = await post('session', { proof: bobProof })
    stage = 'cross-origin-filter'
    const now = Math.floor(Date.now() / 1000)
    const claims = { community: fixture.community, instance: fixture.remote.instance, audience: 'wavechat',
      uuid: alice.player.uuid.replaceAll('-', ''), name: 'RemoteQA', connection: 'f'.repeat(32),
      issued: now, expires: now + 90, nonce: crypto.randomBytes(16).toString('hex') }
    const remoteBody = 'w4ve1.' + Buffer.from(JSON.stringify(claims)).toString('base64url')
    const remoteProof = remoteBody + '.' + crypto.createHmac('sha256', fixture.remote.secret).update(remoteBody).digest('base64url')
    const remoteSession = await post('session', { proof: remoteProof })
    await post('filters', { origin: fixture.remote.instance, mode: 'forever' }, bobSession.token)
    await post('messages', { channel: 'general', body: 'QA_NATIVE_FILTERED', nonce: 'qa-real-filter' }, remoteSession.token)
    const filteredDeadline = Date.now() + 10000
    while (filteredCounts.get(alice) !== 1 && Date.now() < filteredDeadline) await sleep(100)
    if (filteredCounts.get(alice) !== 1 || filteredCounts.get(bob)) throw new Error('muted origin reached an excluded recipient')
    await sleep(1200)
    await post('filters', { origin: fixture.remote.instance, mode: 'on' }, bobSession.token)
    await sleep(1500)
    if (filteredCounts.get(alice) !== 1 || filteredCounts.get(bob)) throw new Error('unmute replayed a skipped live message')
  }
  stage = 'private-controls-respawn-reconnect'
  alice.chat('/msg BridgeQA_B QA_PRIVATE')
  await sleep(500)
  alice.chat('/me QA_COMMAND_ME')
  await sleep(500)
  alice.chat('/say QA_COMMAND_SAY')
  await sleep(500)
  const respawned = once(alice, 'spawn')
  const beforeRespawn = announcements.get(alice) || 0
  alice.chat('/kill')
  await respawned
  await sleep(500)
  if (process.env.QA_REQUIRE_RESPAWN_DISCOVERY === 'true') {
    const discoveryDeadline = Date.now() + 5000
    while ((announcements.get(alice) || 0) <= beforeRespawn && Date.now() < discoveryDeadline) await sleep(50)
  }
  const discoveryRenewed = (announcements.get(alice) || 0) > beforeRespawn
  if (process.env.QA_REQUIRE_RESPAWN_DISCOVERY === 'true' && !discoveryRenewed) throw new Error('respawn did not renew native discovery')
  const respawnContext = await proofFor(alice, 'BridgeQA_A')
  if (respawnContext !== proofs[0]) throw new Error('respawn changed the shared connection identity')
  alice.chat('QA_PUBLIC_RESPAWN')
  await sleep(500)
  await quit(alice)
  alice = await join('BridgeQA_A')
  alice.chat('QA_PUBLIC_RECONNECTED')
  await sleep(500)
  await quit(alice)
  await quit(bob)
  if (process.env.QA_REQUIRE_NATIVE_PAYLOAD === 'true' && (nativeMessages.length !== 2 || new Set(nativeMessages.map(m => m.id)).size !== 2)) throw new Error('typed native messages missing or repeated')
  if (controls.size !== 3) throw new Error('private or command control message did not reach clients')
  fs.writeFileSync('/qa/work/actors-done.json', JSON.stringify({ ok: true, proofs, respawn: true, respawnDiscovery: discoveryRenewed, controls: [...controls].sort(), nativeRelay: process.env.QA_REQUIRE_RELAY === 'true' ? 'passed' : 'not tested', nativeFilterNoReplay: process.env.QA_REQUIRE_RELAY === 'true' ? 'passed' : 'not tested', nativeMessages }))
  clearTimeout(timer)
}
run().catch(fail)
