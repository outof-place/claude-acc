/**
 * claude-acc's Orca plugin in an isolated e2e Orca: installed by claude-acc's own installer
 * (orcaplugin.py, hash-addressed tree), approved through the consent dialog, then the status bar,
 * the live panel, a panel action, a command and the worktree card line. claude-acc itself is faked:
 * state files and a `claude-acc` shim in the isolated HOME.
 */
import { execFileSync } from 'node:child_process'
import { chmodSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { expect, test } from './helpers/orca-app'

// a claude-acc checkout; copy this spec into an Orca checkout's tests/e2e and run it there
const ACC_REPO = process.env.CLAUDE_ACC_REPO ?? ''
const SHOTS = join(ACC_REPO, 'docs')
test.skip(!ACC_REPO, 'CLAUDE_ACC_REPO is not set')
const GB = 1024 ** 3
const MANIFEST = ACC_REPO ? JSON.parse(readFileSync(join(ACC_REPO, 'orca-plugin/orca-plugin.json'), 'utf8')) : { publisher: '', id: '' }
const KEY = `${MANIFEST.publisher}.${MANIFEST.id}`

function seedClaudeAcc(home: string, repoPath: string): string {
  const now = Date.now() / 1000
  const dir = join(home, '.local/share/claude-acc')
  mkdirSync(join(dir, 'sched'), { recursive: true })
  const write = (name: string, data: unknown): void => writeFileSync(join(dir, name), JSON.stringify(data))
  write('status.json', {
    generated_at: now - 20,
    active_email: 'dev@example.com',
    foreign_runtime: false,
    thresholds: { session_left: 5, weekly_left: 3 },
    forecast: { session: { rate: 70, at_reset: 100, switch_at: now + 1500 }, weekly: { rate: 20, at_reset: 100, switch_at: now + 9000 } },
    pause: null,
    limit_pause: false,
    drain: false,
    orca_selected: null,
    accounts: [
      { email: 'dev@example.com', tier: 'Max 20x', active: true, status: 'ok', usable: true, queue: 1, note: '', session: { used: 71, resets_at: now + 5400 }, weekly: { used: 84, resets_at: now + 2 * 86400 } },
      { email: 'spare@example.com', tier: 'Max 5x', active: false, status: 'ok', usable: true, queue: 2, note: 'next in line', session: { used: 12, resets_at: now + 7200 }, weekly: { used: 41, resets_at: now + 86400 } },
      { email: 'old@example.com', tier: 'Pro', active: false, status: 'limited', usable: false, queue: null, note: 'weekly limit', session: { used: 0, resets_at: now + 7200 }, weekly: { used: 100, resets_at: now + 3 * 86400 } }
    ]
  })
  write('devguard-state.json', {
    snapshot: {
      at: now - 2,
      budget: 12 * GB,
      total: 6.4 * GB,
      pressure: { level: 1, stage: 1, stage_reasons: ['swap 6.2 GB and growing'], swap_used: 6.2 * GB },
      units: [
        { key: '1:1', root: 4242, ports: [3000], cwd: [repoPath + '/apps/web'], footprint: 4.6 * GB, attended: true, tabs: [{ focused: true }], clients: [], recyclable: true, servers: 1, worktree: repoPath, launch_cwd: repoPath, pin: null, agent_working: true },
        { key: '2:1', root: 4343, ports: [6006], cwd: [repoPath + '/apps/storybook'], footprint: 1.8 * GB, attended: false, tabs: [], clients: [], recyclable: true, servers: 1, worktree: repoPath, launch_cwd: repoPath, pin: null }
      ],
      plans: [{ unit: '2:1', action: 'stop', code: 'idle', data: { minutes: 25 } }]
    },
    events: []
  })
  write('sched/state.json', {
    memory: { free_for_admission_gb: 9.4, brake: 'normal' },
    running: [{ id: 'j-1791568000-a1b2', label: 'go test ./internal/...', where: 'local', mem_now_gb: 3.2, mem_predicted_gb: 6.1, eta_s: 140, agent: { name: 'builder', worktree: repoPath, pane: null } }],
    queue: [{ id: 'j-1791568100-c3d4', label: 'pnpm tc', position: 1, reason: { text: 'waiting for 12.0 GB, 9.4 free' }, eta_start_s: 140, mem_predicted_gb: 12, waited_s: 35, agent: { name: 'checker', worktree: repoPath, pane: null } }]
  })
  write('perf-state.json', { ultra: { on: true, applied: new Array(14).fill('x'), pending_manual: [], pending_root: [] } })
  write('hotspot-state.json', { enabled: true, active: false, at: now - 1 })
  write('fans-state.json', { at: now - 1, mode: 'auto', percent: 0, cpu: 61, gpu: 48 })
  write('janitor-state.json', { last_sweep: { at: now - 1800, freed: 2.4 * GB }, disk_free: 355 * GB, disk_total: 994 * GB, alerts: [], warnings: [] })
  write('updates-state.json', { last_run: { at: now - 7200, ok: true, failed: 0, updated: 3 }, next_run: now + 86400 })
  write('awake-state.json', { on: true, manual: true, forever: false, until: now + 5400, pid: process.pid })
  const bin = join(home, '.local/bin')
  mkdirSync(bin, { recursive: true })
  const log = join(home, 'acc-calls.log')
  writeFileSync(join(bin, 'claude-acc'), `#!/bin/sh\necho "$*" >> '${log}'\necho "ok: $*"\n`)
  chmodSync(join(bin, 'claude-acc'), 0o755)
  return log
}

test('claude-acc plugin on stock Orca: base manifest, commands, cards, degraded panel', async ({ orcaPage, electronApp }) => {
  test.setTimeout(180_000)
  const { home, userData } = await electronApp.evaluate(({ app }) => ({ home: app.getPath('home'), userData: app.getPath('userData') }))
  await orcaPage.waitForFunction(() => Object.values(window.__store?.getState().worktreesByRepo ?? {}).flat().length > 0)
  const testRepoPath = await orcaPage.evaluate(() => {
    const state = window.__store?.getState()
    const all = Object.values(state?.worktreesByRepo ?? {}).flat()
    return (all.find((w) => w.id === state?.activeWorktreeId) ?? all[0]).path
  })
  const log = seedClaudeAcc(home, testRepoPath)
  await orcaPage.evaluate(async () => {
    const settings = await window.api.settings.set({ pluginSystemEnabled: true })
    window.__store?.setState({ settings })
  })
  // --live auto against this build's main bundle: no panelMessaging, so the stock-safe manifest
  const out = execFileSync('/usr/bin/python3', [join(ACC_REPO, 'orcaplugin.py'), 'install', '--user-data', userData, '--app', '/nonexistent'], { encoding: 'utf8' })
  expect(out).toContain('bez paska statusu')
  const listed = await orcaPage.evaluate(async (key) => (await window.api.plugins.refresh()).find((p) => p.pluginKey === key), KEY)
  expect(listed?.status).toBe('pending')
  await orcaPage.evaluate(() => {
    const state = window.__store?.getState()
    state?.openSettingsTarget({ pane: 'plugins', repoId: null })
    state?.openSettingsPage()
  })
  await orcaPage.getByRole('tab', { name: /^Installed/ }).click()
  const row = orcaPage.locator(`[data-plugin-key="${KEY}"]`)
  await row.getByRole('button', { name: 'Review & enable' }).click()
  const consent = orcaPage.getByRole('dialog', { name: 'Review permissions' })
  await consent.getByRole('button', { name: 'Enable plugin' }).click()
  await expect(row).toContainText('Enabled')
  await orcaPage.evaluate(() => window.__store?.getState().closeSettingsPage())

  // the first command starts the worker; it then runs the CLI and keeps the card line current
  const viaCommand = await orcaPage.evaluate((key) => window.api.plugins.invokeCommand({ pluginKey: key, commandId: 'claude-acc.switch-next' }), KEY)
  expect(viaCommand).toMatchObject({ ok: true })
  expect(readFileSync(log, 'utf8')).toContain('switch --auto')
  const restart = await orcaPage.evaluate((key) => window.api.plugins.invokeCommand({ pluginKey: key, commandId: 'claude-acc.restart-dev-server' }), KEY)
  expect(restart).toMatchObject({ ok: true })
  expect(readFileSync(log, 'utf8')).toContain('guard recycle :3000')
  await expect
    .poll(
      () =>
        orcaPage.evaluate((repo) => {
          const all = Object.values(window.__store?.getState().worktreesByRepo ?? {}).flat()
          return all.find((w) => w.path === repo)?.comment ?? ''
        }, testRepoPath),
      { timeout: 60_000 }
    )
    .toContain('claude-acc: :3000 4.5 GB watched')
  expect(existsSync(log)).toBe(true)

  await orcaPage.evaluate(() => {
    const state = window.__store?.getState()
    if (state && !state.rightSidebarOpen) state.toggleRightSidebar()
  })
  await orcaPage.getByRole('button', { name: 'Claude Acc', exact: true }).click()
  const frame = orcaPage.frameLocator('iframe[title="Claude Acc"]')
  await expect(frame.getByText(/no live plugin panels yet/)).toBeVisible({ timeout: 20_000 })
  await orcaPage.locator('iframe[title="Claude Acc"]').screenshot({ path: join(SHOTS, 'orca-plugin-panel-stock.png') })
})
