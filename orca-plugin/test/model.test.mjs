import assert from 'node:assert/strict'
import test from 'node:test'
import { accountSummary, awakeSummary, buildModel, devUnits, formatDuration, groupByWorktree, memorySummary, schedJobs } from '../lib/model.mjs'
import { GB, NOW, PANE, WORKTREES, guard, job, sched, status, unit } from './fixtures.mjs'

test('durations read like the menu bar', () => {
  assert.equal(formatDuration(42), '42s')
  assert.equal(formatDuration(14 * 60 + 20), '14m')
  assert.equal(formatDuration(2 * 3600 + 5 * 60), '2h 05m')
  assert.equal(formatDuration(3 * 86400 + 4 * 3600), '3d 4h')
  assert.equal(formatDuration(null), null)
  assert.equal(formatDuration(-5), '0s')
})

test('the ring shows the worse window and the first switch', () => {
  const a = accountSummary(status(), NOW)
  assert.equal(a.text, '84%')
  assert.equal(a.window, 'weekly')
  assert.equal(a.severity, 'warning')
  assert.deepEqual([a.countdown.kind, a.countdown.window, a.countdown.text], ['switch', 'session', 'switch in 14m'])
  assert.equal(a.stale, false)
})

test('without a forecast the countdown is the reset of the worse window', () => {
  const a = accountSummary(status({ forecast: null }), NOW)
  assert.deepEqual([a.countdown.kind, a.countdown.text], ['reset', 'resets in 2d 0h'])
})

test('a pause wins over everything and turns the ring red', () => {
  const a = accountSummary(status({ pause: { since: NOW - 60, account: 'a@example.com', resume_at: NOW + 1800 } }), NOW)
  assert.deepEqual([a.countdown.kind, a.countdown.text, a.severity], ['paused', 'resumes in 30m', 'error'])
})

test('a runtime account claude-acc does not manage shows as unknown', () => {
  const s = status({ foreign_runtime: true, accounts: status().accounts.map((x) => ({ ...x, active: false })) })
  const a = accountSummary(s, NOW)
  assert.deepEqual([a.text, a.foreign, a.severity], ['?', true, 'warning'])
})

test('an old status.json is stale', () => {
  assert.equal(accountSummary(status({ generated_at: NOW - 3600 }), NOW).stale, true)
  assert.equal(accountSummary(null, NOW), null)
})

test('memory brake stage maps to severity', () => {
  assert.equal(memorySummary(guard({ stage: 0 }), sched(), NOW).severity, 'normal')
  const tight = memorySummary(guard({ stage: 1 }), sched(), NOW)
  assert.deepEqual([tight.severity, tight.stageName, tight.reasons], ['warning', 'tight', ['swap 9.1 GB and rising']])
  assert.equal(memorySummary(guard({ stage: 2 }), sched(), NOW).severity, 'error')
  assert.equal(memorySummary(guard({ stage: 3 }), sched(), NOW).stageName, 'emergency')
  assert.equal(memorySummary(null, null, NOW).stale, true)
})

test('dev server units carry the guard plan and the CLI target', () => {
  const units = devUnits(guard({
    units: [unit(), unit({ key: 'k2', root: 5, ports: [], attended: true, tabs: [{ focused: true }] })],
    plans: [{ unit: '81617:1', action: 'recycle', code: 'bloated', data: { size: 7.5 * GB } }]
  }))
  assert.equal(units[0].plan.text, 'Restart when quiet · grew to 7.5 GB')
  assert.deepEqual([units[0].target, units[0].portLabel, units[0].gb, units[0].viewers], [':3000', ':3000', '4.2 GB', 'nobody'])
  assert.deepEqual([units[1].target, units[1].portLabel, units[1].viewers], ['5', 'pid 5', 'you'])
  assert.equal(units[0].pinTarget, ':3000')
})

test('a stack is titled Dev stack', () => {
  const [stack] = devUnits(guard({ units: [unit({ servers: 6, ports: [3000, 3002] })] }))
  assert.deepEqual([stack.title, stack.portLabel], ['Dev stack', ':3000 +1'])
})

test('units and jobs group under the deepest Orca worktree; panes win over paths', () => {
  const units = devUnits(guard({ units: [unit(), unit({ key: 'c', worktree: '/w/app/.wt/child' }), unit({ key: 'x', worktree: '/elsewhere' })] }))
  const jobs = schedJobs(sched({
    running: [job()],
    queue: [job({ id: 'j-2-0001', agent: { worktree: '~/w/other/sub', pane: null }, position: 1, reason: { text: 'waiting for 8.3 GB' }, eta_start_s: 60 })]
  }), NOW)
  const panes = new Map([[PANE, 'repo::/w/app/.wt/child']])
  const g = groupByWorktree(units, jobs, WORKTREES, panes, '')
  const by = Object.fromEntries(g.worktrees.map((w) => [w.name, w]))
  assert.deepEqual(by.app.units.map((u) => u.key), ['81617:1'])
  assert.deepEqual(by.child.units.map((u) => u.key), ['c'])
  assert.deepEqual(by.child.jobs.map((j) => j.id), ['j-1791567988-a461'])
  assert.deepEqual(g.elsewhere.units.map((u) => u.key), ['x'])
  // the queued job has no pane and a ~ path; with HOME expanded it lands in "other"
  const withHome = groupByWorktree([], jobs.slice(1), [{ ...WORKTREES[2], path: '/home/me/w/other' }], new Map(), '/home/me')
  assert.equal(withHome.worktrees[0].jobs[0].reason, 'waiting for 8.3 GB')
  assert.equal(withHome.worktrees[0].jobs[0].eta, '1m')
})

test('a prefix that is not a parent folder does not match', () => {
  const g = groupByWorktree(devUnits(guard({ units: [unit({ worktree: '/w/application' })] })), [], WORKTREES)
  assert.equal(g.worktrees.length, 0)
  assert.equal(g.elsewhere.units.length, 1)
})

test('stay awake reads the app state and its liveness', () => {
  assert.equal(awakeSummary({ on: true, manual: true, forever: true, running: true }, NOW).text, 'Awake')
  assert.equal(awakeSummary({ on: true, manual: true, until: NOW + 3600, running: true }, NOW).text, 'Awake for 1h 00m')
  assert.equal(awakeSummary({ on: true, hotspot: true, running: true }, NOW).text, 'Awake on hotspot')
  const dead = awakeSummary({ on: true, manual: true, running: false }, NOW)
  assert.deepEqual([dead.on, dead.text], [false, 'App not running'])
})

test('the whole model from files', () => {
  const m = buildModel({
    status: status(), guard: guard({ stage: 1 }), sched: sched({ running: [job()] }),
    perf: { ultra: { on: true, applied: ['a', 'b'], pending_manual: ['x'], pending_root: [] } },
    hotspot: { enabled: true, active: true, at: NOW - 1, via: 'Wi-Fi', rate_kbps: 37000, delay_p50_ms: 11.8 },
    fans: { at: NOW - 100, mode: 'fixed', percent: 100 },
    janitor: { last_sweep: { at: NOW - 600, freed: 2 * GB }, alerts: [], warnings: ['w'] },
    updates: { last_run: { at: NOW - 3600, ok: false, failed: 1, updated: 10 }, next_run: NOW + 86400 },
    awake: { on: false, running: true }
  }, { worktrees: WORKTREES, panes: new Map(), home: '' }, NOW)
  assert.equal(m.account.text, '84%')
  assert.equal(m.accounts.length, 2)
  assert.equal(m.memory.stage, 1)
  assert.equal(m.worktrees[0].name, 'app')
  assert.deepEqual(m.health.ultra, { on: true, pendingManual: 1, pendingRoot: 0, applied: 2 })
  assert.equal(m.health.hotspot.active, true)
  assert.equal(m.health.fans.running, false)
  assert.equal(m.health.updates.ok, false)
  assert.equal(m.health.awake.text, 'Off')
})
