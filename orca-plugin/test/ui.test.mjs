// Status bar items, worktree card lines, notifications and the action allowlist.
import assert from 'node:assert/strict'
import test from 'node:test'
import { actionArgv } from '../lib/actions.mjs'
import { CARD_PREFIX, cardLine, mergeComment } from '../lib/cards.mjs'
import { buildModel, devUnits } from '../lib/model.mjs'
import { noticeBasis, notifications } from '../lib/notify.mjs'
import { statusBarItems } from '../lib/statusbar.mjs'
import { GB, NOW, WORKTREES, guard, job, sched, status, unit } from './fixtures.mjs'

function model(files = {}, now = NOW) {
  return buildModel({ status: status(), guard: guard(), sched: sched(), ...files }, { worktrees: WORKTREES, panes: new Map(), home: '' }, now)
}

test('status bar: ring %, countdown, brake badge only when braking, awake only when on', () => {
  const items = Object.fromEntries(statusBarItems(model()).map((i) => [i.id, i]))
  assert.equal(items.account.text, 'Claude 84% · switch 14m')
  assert.equal(items.account.severity, 'warning')
  assert.ok(!items.account.tooltip.includes('\n'))
  assert.match(items.account.tooltip, /a@example\.com \(Max 20x\) · Session: 77% used, resets in 1h 00m · Weekly: 84% used/)
  assert.equal(items.memory.visible, false)
  assert.equal(items.awake.visible, false)
  const braking = Object.fromEntries(statusBarItems(model({ guard: guard({ stage: 2 }), awake: { on: true, manual: true, forever: true, running: true } })).map((i) => [i.id, i]))
  assert.deepEqual([braking.memory.visible, braking.memory.severity, braking.memory.text], [true, 'error', 'Memory brake'])
  assert.deepEqual([braking.awake.visible, braking.awake.text], [true, 'Awake'])
  for (const item of statusBarItems(model())) {
    assert.match(item.id, /^[a-z0-9]+(?:-[a-z0-9]+)*$/)
    assert.ok(item.text.length <= 80 && item.tooltip.length <= 512)
  }
})

test('status bar without status.json', () => {
  const [account] = statusBarItems(model({ status: null }))
  assert.deepEqual([account.text, account.severity], ['Claude –', 'warning'])
})

test('card line: rough GB, watched, plans and builds', () => {
  const units = devUnits(guard({
    units: [unit({ footprint: 4.2 * GB, attended: true }), unit({ key: 'k2', ports: [3001], footprint: 1.1 * GB })],
    plans: [{ unit: 'k2', action: 'stop', code: 'idle', data: { minutes: 30 } }]
  }))
  const line = cardLine({ units, jobs: [{ running: true }, { running: false }, { running: false }] })
  assert.equal(line, `${CARD_PREFIX} :3000 4.0 GB watched · :3001 1.0 GB, stop planned · 1 build running · 2 queued`)
  assert.equal(cardLine({ units: [], jobs: [] }), null)
})

test('a 0.1 GB wobble does not change the card line', () => {
  const a = cardLine({ units: devUnits(guard({ units: [unit({ footprint: 4.1 * GB })] })), jobs: [] })
  const b = cardLine({ units: devUnits(guard({ units: [unit({ footprint: 4.2 * GB })] })), jobs: [] })
  assert.equal(a, b)
})

test('merge comment: our line only, devguard notes kept, user comments never touched', () => {
  const line = `${CARD_PREFIX} :3000 4.0 GB`
  assert.equal(mergeComment('', line), line)
  assert.equal(mergeComment(line, line), null)
  assert.equal(mergeComment(`${CARD_PREFIX} old`, line), line)
  assert.equal(mergeComment('devguard: stopped :3001 (idle)', line), `devguard: stopped :3001 (idle)\n${line}`)
  assert.equal(mergeComment('reviewing the auth flow', line), null)
  assert.equal(mergeComment(`${line}\nmy note`, null), null)
  assert.equal(mergeComment(line, null), '')
  assert.equal(mergeComment('', null), null)
  assert.equal(mergeComment(undefined, null), null)
})

test('notifications: limit crossing once, switch soon, switched, paused, brake rise', () => {
  const sent = {}
  const before = noticeBasis(model({ status: status({ accounts: status().accounts.map((a) => (a.active ? { ...a, weekly: { used: 85, resets_at: NOW + 9e4 } } : a)) }) }))
  const hot = model({ status: status({ accounts: status().accounts.map((a) => (a.active ? { ...a, weekly: { used: 91, resets_at: NOW + 9e4 } } : a)), forecast: { session: { switch_at: NOW + 300 } } }) })
  const first = notifications(before, hot, sent).map((n) => n.title)
  assert.deepEqual(first, ['Claude weekly limit at 91%', 'Claude limit approaching'])
  assert.deepEqual(notifications(noticeBasis(hot), hot, sent), [])
  // after a worker restart the stored keys still dedupe
  assert.deepEqual(notifications(before, hot, sent), [])

  const switched = model({ status: status({ accounts: status().accounts.map((a) => ({ ...a, active: a.email === 'b@example.com' })) }) })
  assert.deepEqual(notifications(noticeBasis(model()), switched, {}).map((n) => n.title), ['Switched to b@example.com'])

  const paused = model({ status: status({ pause: { resume_at: NOW + 600 } }) })
  assert.ok(notifications(noticeBasis(model()), paused, {}).some((n) => n.title === 'Claude sessions paused' && n.body.includes('resumes in 10m')))

  const brake = model({ guard: guard({ stage: 2 }) })
  assert.deepEqual(notifications(noticeBasis(model({ guard: guard({ stage: 1 }) })), brake, {}).map((n) => [n.title, n.body]), [['Memory brake', 'swap 9.1 GB and rising']])
  assert.deepEqual(notifications(noticeBasis(brake), model({ guard: guard({ stage: 1 }) }), {}), [])
  // a first run has nothing to compare against: no switch or brake notice out of the blue
  assert.deepEqual(notifications(null, brake, {}), [])
})

test('actions: closed list, checked arguments', () => {
  assert.deepEqual(actionArgv('switch', { email: 'auto' }).argv, ['switch', '--auto'])
  assert.deepEqual(actionArgv('switch', { email: 'b@example.com' }).argv, ['switch', 'b@example.com'])
  assert.deepEqual(actionArgv('guard', { verb: 'recycle', target: ':3000' }).argv, ['guard', 'recycle', ':3000'])
  assert.deepEqual(actionArgv('guard', { verb: 'pin', target: '/w/app' }).argv, ['guard', 'pin', '/w/app'])
  assert.deepEqual(actionArgv('cancel', { target: 'j-1791567988-a461' }).argv, ['sched', 'cancel', 'j-1791567988-a461', '--json'])
  assert.deepEqual(actionArgv('awake', { on: true, for: 3600 }).argv, ['awake', 'on', '--for', '3600'])
  assert.deepEqual(actionArgv('ultra', { on: false }), { argv: ['perf', 'ultra', 'off'], long: true })
  assert.equal(actionArgv('clean').long, true)
  assert.deepEqual(actionArgv('panel', { section: 'services' }).argv, ['panel', 'services'])
  assert.deepEqual(actionArgv('panel').argv, ['panel'])
  for (const [action, args] of [
    ['guard', { verb: 'stop', target: ':3000; rm -rf ~' }],
    ['guard', { verb: 'recycle', target: '/w/app' }],
    ['guard', { verb: 'kill', target: ':3000' }],
    ['switch', { email: '--auto; x' }],
    ['cancel', { target: 'all' }],
    ['awake', { on: true, for: '1h' }],
    ['panel', { section: '../../etc' }],
    ['shell', {}]
  ]) {
    assert.throws(() => actionArgv(action, args), undefined, `${action} ${JSON.stringify(args)}`)
  }
})

test('job rows say what they wait for', () => {
  const m = model({ sched: sched({ queue: [job({ position: 2, reason: { text: 'waiting for 8.3 GB, 4.0 free' }, eta_start_s: 130, waited_s: 40 })] }) })
  // no pane mapping and a ~ path without HOME: the job lands outside Orca worktrees, still listed
  const [j] = m.elsewhere.jobs
  assert.deepEqual(m.jobs.map((x) => x.id), [j.id])
  assert.deepEqual([j.running, j.reason, j.eta, j.waited, j.position], [false, 'waiting for 8.3 GB, 4.0 free', '2m', '40s', 2])
})
