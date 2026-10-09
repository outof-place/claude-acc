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

test('claude-acc plugin: status bar, live panel, actions and worktree card', async ({ orcaPage, electronApp }) => {
  test.setTimeout(180_000)
  const { home, userData } = await electronApp.evaluate(({ app }) => ({ home: app.getPath('home'), userData: app.getPath('userData') }))
  // the path exactly as Orca stores it (tmp paths can differ by /private)
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
  // claude-acc's installer, not Orca's: the hash-addressed tree must pass Orca's own discovery and integrity check
  const out = execFileSync('/usr/bin/python3', [join(ACC_REPO, 'orcaplugin.py'), 'install', '--user-data', userData, '--live', 'on'], { encoding: 'utf8' })
  expect(out).toContain('z paskiem statusu')
  const listed = await orcaPage.evaluate(async () => (await window.api.plugins.refresh()).find((p) => p.pluginKey === 'outof-place.claude-acc'))
  expect(listed?.status).toBe('pending')

  await orcaPage.evaluate(() => {
    const state = window.__store?.getState()
    state?.openSettingsTarget({ pane: 'plugins', repoId: null })
    state?.openSettingsPage()
  })
  await orcaPage.getByRole('tab', { name: /^Installed/ }).click()
  const row = orcaPage.locator('[data-plugin-key="outof-place.claude-acc"]')
  await row.getByRole('button', { name: 'Review & enable' }).click()
  const consent = orcaPage.getByRole('dialog', { name: 'Review permissions' })
  await expect(consent).toBeVisible()
  await consent.getByRole('button', { name: 'Enable plugin' }).click()
  await expect(row).toContainText('Enabled')
  await orcaPage.evaluate(() => window.__store?.getState().closeSettingsPage())

  const account = orcaPage.locator('[data-plugin-status-item="outof-place.claude-acc/account"]')
  await expect(account).toContainText(/Claude 84% · switch 2[45]m/, { timeout: 30_000 })
  const memory = orcaPage.locator('[data-plugin-status-item="outof-place.claude-acc/memory"]')
  await expect(memory).toContainText('Memory tight')
  await expect(memory).toHaveAttribute('data-severity', 'warning')
  await expect(orcaPage.locator('[data-plugin-status-item="outof-place.claude-acc/awake"]')).toContainText('Awake for 1h')
  await account.locator('xpath=..').screenshot({ path: join(SHOTS, 'orca-plugin-statusbar.png') })

  // a status bar click opens the panel; the worker pushes the model into it
  await account.click()
  const frame = orcaPage.frameLocator('iframe[title="Claude Acc"]')
  await expect(frame.getByText('dev@example.com')).toBeVisible({ timeout: 20_000 })
  await expect(frame.getByText('Will stop · idle for 25 min')).toBeVisible()
  await expect(frame.getByText('pnpm tc')).toBeVisible()
  await expect(frame.getByText(/#1 · waiting for 12.0 GB, 9.4 free/)).toBeVisible()
  const sidebar = orcaPage.locator('iframe[title="Claude Acc"]')
  await sidebar.screenshot({ path: join(SHOTS, 'orca-plugin-panel.png') })

  await frame.getByRole('button', { name: 'Switch', exact: true }).first().click()
  await expect(frame.getByText('ok: switch spare@example.com')).toBeVisible()
  await frame.getByRole('button', { name: 'Cancel' }).click()
  await expect.poll(() => (existsSync(log) ? readFileSync(log, 'utf8') : '')).toContain('sched cancel j-1791568100-c3d4 --json')

  const viaCommand = await orcaPage.evaluate(() => window.api.plugins.invokeCommand({ pluginKey: 'outof-place.claude-acc', commandId: 'claude-acc.switch-next' }))
  expect(viaCommand).toMatchObject({ ok: true })
  expect(readFileSync(log, 'utf8')).toContain('switch --auto')

  // the card line lands on the seeded repo's worktree, through Orca's runtime RPC
  await expect
    .poll(
      () =>
        orcaPage.evaluate((repo) => {
          const state = window.__store?.getState()
          const all = Object.values(state?.worktreesByRepo ?? {}).flat()
          return all.find((w) => w.path === repo)?.comment ?? ''
        }, testRepoPath),
      { timeout: 60_000 }
    )
    .toContain('claude-acc: :3000 4.5 GB watched · :6006 2.0 GB, stop planned · 1 build running · 1 queued')
})
