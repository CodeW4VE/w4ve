// TCP clients for the network-none disposable fixture, never a public server.
const mineflayer = require('/qa/node/node_modules/mineflayer')
const { once } = require('node:events')
const fs = require('node:fs')
const crypto = require('node:crypto')
// Generated only inside the disposable QA fixture. Never a production issuer.
const fixturePath = '/qa/work/fixture-identity.json'
const fixture = fs.existsSync(fixturePath) ? JSON.parse(fs.readFileSync(fixturePath, 'utf8')) : {
  secret: 'QA_ONLY_NOT_A_REAL_SIGNING_SECRET_20261015', community: 'bridge-qa', instance: 'fabric-qa'
}
const bots = new Set()
const proofs = []
const controls = new Set()
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
const timer = setTimeout(() => fail(), 90000)
function fail () {
  fs.writeFileSync('/qa/work/actors-done.json', JSON.stringify({ ok: false }))
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
    for (const marker of ['QA_PRIVATE', 'QA_COMMAND_ME', 'QA_COMMAND_SAY']) {
      if (text.includes(marker)) controls.add(marker)
    }
  })
  bot.on('error', fail)
  bot.on('kicked', fail)
  await once(bot, 'spawn')
  bot.physicsEnabled = false
  bot._client.registerChannel('wavechat:identity_grant', 'string', true)
  proofs.push(await proofFor(bot, username))
  return bot
}
async function proofFor (bot, username) {
  await sleep(300)
  const received = once(bot._client, 'wavechat:identity_grant')
  bot._client.write('custom_payload', { channel: 'wavechat:identity_request', data: Buffer.alloc(0) })
  const [proof] = await received
  const [prefix, body, signature] = proof.split('.')
  const expected = crypto.createHmac('sha256', fixture.secret).update(prefix + '.' + body).digest('base64url')
  if (prefix !== 'w4ve1' || signature !== expected) throw new Error('invalid fixture proof')
  const actor = JSON.parse(Buffer.from(body, 'base64url').toString('utf8'))
  if (actor.name !== username || actor.community !== fixture.community || actor.instance !== fixture.instance || !/^[a-f0-9]{32}$/.test(actor.connection)) throw new Error('missing fixture context')
  return actor.connection // Never persist the full capability.
}
async function quit (bot) {
  const ended = once(bot, 'end')
  bot.quit()
  await ended
  bots.delete(bot)
  await sleep(400)
}
async function run () {
  let alice = await join('BridgeQA_A')
  const bob = await join('BridgeQA_B')
  alice.chat('QA_PUBLIC_FIRST')
  await sleep(500)
  bob.chat('QA_PUBLIC_BOB')
  await sleep(500)
  alice.chat('/msg BridgeQA_B QA_PRIVATE')
  await sleep(500)
  alice.chat('/me QA_COMMAND_ME')
  await sleep(500)
  alice.chat('/say QA_COMMAND_SAY')
  await sleep(500)
  const respawned = once(alice, 'spawn')
  alice.chat('/kill')
  await respawned
  await sleep(500)
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
  if (controls.size !== 3) throw new Error('private or command control message did not reach clients')
  fs.writeFileSync('/qa/work/actors-done.json', JSON.stringify({ ok: true, proofs, respawn: true, controls: [...controls].sort() }))
  clearTimeout(timer)
}
run().catch(fail)
