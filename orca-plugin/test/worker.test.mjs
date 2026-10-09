// The worker against a fake Orca: with the live APIs (status bar, panel messaging) and without (stock Orca).
import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { StatePoller } from '../lib/files.mjs'
import { findRuntime, rpcRequest } from '../lib/orca-rpc.mjs'
import { AccPlugin, PANEL_ID } from '../worker.mjs'
import { NOW, guard, job, sched, status } from './fixtures.mjs'

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)))
const LEAF = '11111111-1111-4111-8111-111111111111'

function stateDir(files) {
  const dir = mkdtempSync(join(tmpdir(), 'acc-plugin-'))
  for (const [name, data] of Object.entries(files)) {
    mkdirSync(dirname(join(dir, name)), { recursive: true })
    writeFileSync(join(dir, name), JSON.stringify(data))
  }
  return dir
}

function harness({ live = true, files = {}, comments = {}, context } = {}) {
  const dir = stateDir({
    'status.json': status(),
    'devguard-state.json': guard(),
    // the job's pane is a terminal of /w/app in Orca's terminal list; its ~ path alone would not match
    'sched/state.json': sched({ running: [job({ agent: { worktree: '~/w/app', pane: `tab-9:${LEAF}` } })] }),
    ...files
  })
  const h = { calls: [], commands: new Map(), events: new Map(), bar: [], panel: [], onPanel: null, runs: [], sets: [], logs: [] }
  const orca = {
    commands: { register: (id, fn) => h.commands.set(id, fn) },
    events: { on: (name, fn) => h.events.set(name, fn) },
    host: {
      call: async (method, params) => {
        h.calls.push([method, params])
        if (method === 'storage.get') return { value: h.stored ?? null }
        if (method === 'storage.set') h.stored = params.value
        if (method === 'workspace.readContext') return context === undefined ? { branch: 'main', displayName: 'app', terminals: [{ id: 'term_a' }] } : context
        return { ok: true }
      }
    },
    grantedCapabilities: [],
    log: (m) => h.logs.push(m)
  }
  if (live) {
    orca.statusBar = { update: async (id, item) => h.bar.push([id, item]) }
    orca.panels = { postMessage: async (id, msg) => h.panel.push([id, msg]), onMessage: (id, fn) => { h.onPanel = [id, fn] } }
  }
  const worktrees = [
    { id: 'repo::/w/app', path: '/w/app', displayName: 'app', branch: 'refs/heads/main', comment: comments.app ?? '', hostId: 'local' },
    { id: 'repo::/w/other', path: '/w/other', displayName: 'other', branch: 'refs/heads/other', comment: comments.other ?? 'my own note', hostId: 'local' }
  ]
  const rpc = {
    call: async (method, params) => {
      if (method === 'worktree.list') {
        h.lists = (h.lists ?? 0) + 1
        h.onList?.(worktrees, h.lists)
        return { worktrees: worktrees.map((w) => ({ ...w })) }
      }
      if (method === 'terminal.list') return { terminals: [{ handle: 'term_a', tabId: 'tab-9', leafId: LEAF, worktreeId: 'repo::/w/app', worktreePath: '/w/app' }] }
      if (method === 'worktree.set') {
        h.sets.push(params)
        return { worktree: {} }
      }
      throw new Error(`unexpected ${method}`)
    }
  }
  const run = async (argv) => {
    h.runs.push(argv)
    return { ok: true, code: 0, stdout: argv[0] === 'sched' ? JSON.stringify({ jobs: [{ result: 'dequeued' }] }) : '', stderr: '', message: `ran ${argv.join(' ')}` }
  }
  h.plugin = new AccPlugin(orca, { poller: new StatePoller(dir), rpc, run, now: () => NOW, home: '/nonexistent' })
  h.plugin.register()
  h.cleanup = () => rmSync(dir, { recursive: true, force: true })
  return h
}

test('every worker command in the manifest has a handler', () => {
  const manifest = JSON.parse(readFileSync(join(ROOT, 'orca-plugin.json'), 'utf8'))
  const live = JSON.parse(readFileSync(join(ROOT, 'live-features.json'), 'utf8'))
  const h = harness()
  const declared = manifest.contributes.commands.filter((c) => !c.action).map((c) => c.id).sort()
  assert.deepEqual([...h.commands.keys()].sort(), declared)
  const ids = new Set(manifest.contributes.commands.map((c) => c.id))
  const panels = new Set(manifest.contributes.panels.map((p) => p.id))
  for (const item of live.contributes.statusBarItems) assert.ok(item.panel ? panels.has(item.panel) : ids.has(item.command), item.id)
  h.cleanup()
})

test('live Orca: status bar items, panel model and one card line', async () => {
  const h = harness()
  await h.plugin.tick()
  assert.deepEqual(h.bar.map(([id]) => id), ['account', 'memory', 'awake'])
  assert.equal(h.bar[0][1].text, 'Claude 84% · switch 14m')
  const [panelId, msg] = h.panel.at(-1)
  assert.equal(panelId, PANEL_ID)
  assert.equal(msg.type, 'model')
  assert.equal(msg.model.worktrees[0].name, 'app')
  assert.deepEqual(msg.features, { statusBar: true, livePanel: true, rpc: true })
  // the dev server and the running build sit in /w/app; /w/other has the user's own comment
  assert.deepEqual(h.sets, [{ worktree: 'id:repo::/w/app', comment: 'claude-acc: :3000 4.0 GB · 1 build running' }])
  // nothing changed: no second status bar update, no second card write
  h.bar.length = 0
  h.plugin.lastPublish = 0
  await h.plugin.tick()
  assert.deepEqual(h.bar, [])
  assert.equal(h.sets.length, 1)
  h.cleanup()
})

test('a comment typed since the last refresh is read again and left alone', async () => {
  const h = harness()
  // the first list (the 30 s refresh) still has the empty card; the user types before the write
  h.onList = (worktrees, n) => {
    if (n >= 2) worktrees[0].comment = 'reviewing the auth flow'
  }
  await h.plugin.tick()
  assert.deepEqual(h.sets, [])
  assert.ok(h.lists >= 2)
  h.cleanup()
})

test('panel actions run through the allowlist and answer the panel', async () => {
  const h = harness()
  await h.plugin.tick()
  const [, onMessage] = h.onPanel
  await onMessage({ type: 'action', id: 'a1', action: 'guard', args: { verb: 'recycle', target: ':3000' } })
  assert.deepEqual(h.runs, [['guard', 'recycle', ':3000']])
  assert.deepEqual(h.panel.find(([, m]) => m.type === 'result')[1], { type: 'result', id: 'a1', ok: true, message: 'ran guard recycle :3000' })
  await onMessage({ type: 'action', id: 'a2', action: 'guard', args: { verb: 'stop', target: '$(reboot)' } })
  assert.equal(h.runs.length, 1)
  assert.equal(h.panel.filter(([, m]) => m.type === 'result').at(-1)[1].ok, false)
  await onMessage({ type: 'prefs', prefs: { cards: false, evil: 1 } })
  assert.deepEqual(h.stored.prefs, { cards: false, notifications: true })
  await onMessage({ type: 'hello' })
  assert.equal(h.panel.at(-1)[1].type, 'model')
  h.cleanup()
})

test('stock Orca: no status bar or panel, commands act on the focused worktree', async () => {
  const h = harness({ live: false })
  await h.plugin.tick()
  assert.deepEqual([h.bar, h.panel], [[], []])
  await h.commands.get('claude-acc.restart-dev-server')()
  assert.deepEqual(h.runs, [['guard', 'recycle', ':3000']])
  const shown = h.calls.filter(([m]) => m === 'notifications.show').map(([, p]) => p)
  assert.equal(shown.at(-1).title, 'Restart in app')
  await h.commands.get('claude-acc.cancel-builds')()
  assert.deepEqual(h.runs.at(-1), ['sched', 'cancel', 'j-1791567988-a461', '--json'])
  assert.match(h.calls.filter(([m]) => m === 'notifications.show').at(-1)[1].body, /go test \.\/\.\.\.: dequeued/)
  h.cleanup()
})

test('a command with no worktree in focus says so', async () => {
  const h = harness({ live: false, context: null })
  await h.plugin.tick()
  await h.commands.get('claude-acc.stop-dev-server')()
  assert.deepEqual(h.runs, [])
  assert.equal(h.calls.filter(([m]) => m === 'notifications.show').at(-1)[1].title, 'No worktree in focus')
  h.cleanup()
})

test('long actions start in the background and report back', async () => {
  const h = harness({ live: false })
  const result = await h.commands.get('claude-acc.clean')()
  assert.deepEqual(result, { started: true })
  await new Promise((r) => setTimeout(r, 10))
  const titles = h.calls.filter(([m]) => m === 'notifications.show').map(([, p]) => p.title)
  assert.deepEqual(titles.slice(0, 2), ['claude-acc clean', 'claude-acc clean'])
  h.cleanup()
})

test('a brake rising to stage 2 notifies once', async () => {
  const h = harness({ live: false })
  await h.plugin.tick()
  writeFileSync(join(h.plugin.poller.dir, 'devguard-state.json'), JSON.stringify(guard({ stage: 2 })))
  await h.plugin.tick()
  await h.plugin.tick()
  const brakes = h.calls.filter(([m, p]) => m === 'notifications.show' && p.title === 'Memory brake')
  assert.equal(brakes.length, 1)
  h.cleanup()
})

test('owner RPC: NDJSON request with the token, keepalives skipped, runtime found by its folder', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'acc-rpc-'))
  const endpoint = join(dir, `o-${process.pid}-abcd.sock`)
  const seen = []
  const server = createServer((socket) => {
    socket.setEncoding('utf8')
    socket.on('data', (line) => {
      const req = JSON.parse(line)
      seen.push(req)
      socket.write('{"_keepalive":true}\n')
      if (req.method === 'boom') socket.write(`${JSON.stringify({ id: req.id, ok: false, error: { code: 'selector_not_found', message: 'nope' } })}\n`)
      else socket.write(`${JSON.stringify({ id: req.id, ok: true, result: { echo: req.params } })}\n`)
    })
  })
  await new Promise((r) => server.listen(endpoint, r))
  writeFileSync(join(dir, 'orca-runtime.json'), JSON.stringify({ pid: 1, authToken: 'tok', transports: [{ kind: 'unix', endpoint }] }))
  const runtime = await findRuntime({ userData: dir, home: '/nonexistent' })
  assert.equal(runtime.dir, dir)
  assert.deepEqual(await rpcRequest(runtime.meta, 'worktree.list', { limit: 5 }), { echo: { limit: 5 } })
  assert.deepEqual([seen[0].authToken, seen[0].method], ['tok', 'worktree.list'])
  await assert.rejects(rpcRequest(runtime.meta, 'boom', {}), (e) => e.code === 'selector_not_found')
  assert.equal(await findRuntime({ home: '/nonexistent', ppid: 999_999 }), null)
  server.close()
  rmSync(dir, { recursive: true, force: true })
})
