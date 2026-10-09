// State files as claude-acc writes them (shapes from status.json, devguard-state.json, sched/state.json).
export const NOW = 1_791_568_000
export const GB = 1024 ** 3

export function status(overrides = {}) {
  return {
    generated_at: NOW - 30,
    active_email: 'a@example.com',
    foreign_runtime: false,
    thresholds: { session_left: 5, weekly_left: 3 },
    forecast: {
      session: { rate: 72.6, at_reset: 100, switch_at: NOW + 840 },
      weekly: { rate: 19.8, at_reset: 100, switch_at: NOW + 2400 }
    },
    pause: null,
    limit_pause: false,
    drain: false,
    orca_selected: null,
    accounts: [
      {
        email: 'a@example.com', tier: 'Max 20x', active: true, status: 'ok', usable: true, queue: 1, note: '',
        session: { used: 77, resets_at: NOW + 3600 }, weekly: { used: 84, resets_at: NOW + 86400 * 2 }
      },
      {
        email: 'b@example.com', tier: 'Max 5x', active: false, status: 'ok', usable: true, queue: 2, note: 'spare',
        session: { used: 10, resets_at: NOW + 7200 }, weekly: { used: 40, resets_at: NOW + 86400 }
      }
    ],
    ...overrides
  }
}

export function unit(overrides = {}) {
  return {
    key: '81617:1', root: 81617, ports: [3000], kinds: ['next'], cwd: ['/w/app/apps/web'], footprint: 4.2 * GB,
    peak: 5 * GB, host: 'shell', command: 'pnpm dev', terminal: 'Terminal 2', clients: [], tabs: [], attended: false,
    agent_working: true, recyclable: true, quiet: 10, protected: false, servers: 1, worktree: '/w/app',
    launch_cwd: '/w/app', pin: null,
    ...overrides
  }
}

export function guard({ stage = 0, units = [unit()], plans = [] } = {}) {
  return {
    snapshot: {
      at: NOW - 2,
      budget: 12 * GB,
      total: units.reduce((n, u) => n + u.footprint, 0),
      pressure: { level: stage >= 2 ? 2 : 0, stage, stage_reasons: stage ? ['swap 9.1 GB and rising'] : [], swap_used: 3 * GB },
      units,
      plans
    },
    events: []
  }
}

export function sched({ running = [], queue = [] } = {}) {
  return { memory: { free_for_admission_gb: 17.9, brake: 'normal' }, running, queue }
}

export const PANE = 'tab-1:11111111-1111-4111-8111-111111111111'

export function job(overrides = {}) {
  return {
    id: 'j-1791567988-a461', label: 'go test ./...', where: 'local', mem_now_gb: 2.1, mem_predicted_gb: 6, eta_s: 95,
    agent: { session: 'abcd1234', name: null, worktree: '~/w/app', pane: PANE },
    ...overrides
  }
}

export const WORKTREES = [
  { id: 'repo::/w/app', path: '/w/app', displayName: 'app', branch: 'refs/heads/main', comment: '' },
  { id: 'repo::/w/app/.wt/child', path: '/w/app/.wt/child', displayName: 'child', branch: 'refs/heads/child', comment: '' },
  { id: 'repo::/w/other', path: '/w/other', displayName: 'other', branch: 'refs/heads/other', comment: '' }
]
