// Pure view model of claude-acc's state files: no I/O, so tests feed it fixtures. Mirrors what the
// menu bar app shows (app/Sources/ClaudeAcc), in the same English wording.

const GB = 1024 ** 3
export const STAGE_NAMES = ['calm', 'tight', 'brake', 'emergency']
// status.json is rewritten by every accswitch tick (2 min): older than this, the tick is not running
const STATUS_STALE_S = 10 * 60
// devguard writes every 5 s, the hotspot and fan daemons every 2 s
const GUARD_STALE_S = 60
const DAEMON_STALE_S = 30

/** "45s", "14m", "2h 05m", "3d 4h" from seconds; null for a missing value. */
export function formatDuration(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return null
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) {
    const h = Math.floor(s / 3600)
    const m = Math.floor((s % 3600) / 60)
    return `${h}h ${String(m).padStart(2, '0')}m`
  }
  const d = Math.floor(s / 86400)
  return `${d}d ${Math.floor((s % 86400) / 3600)}h`
}

export function formatGb(bytes) {
  if (bytes == null || !Number.isFinite(bytes)) return null
  const gb = bytes / GB
  return gb >= 10 ? `${Math.round(gb)} GB` : `${gb.toFixed(1)} GB`
}

/** Same thresholds as the menu bar ring: violet below 75%, orange below 90%, red above. */
export function usageSeverity(used) {
  if (used == null) return 'normal'
  if (used >= 90) return 'error'
  if (used >= 75) return 'warning'
  return 'normal'
}

function windowView(raw) {
  if (!raw || typeof raw.used !== 'number') return null
  return { used: raw.used, resetsAt: typeof raw.resets_at === 'number' ? raw.resets_at : null }
}

/** The active account as the status bar shows it: the worse window and when the wall or the switch comes. */
export function accountSummary(status, now) {
  if (!status || !Array.isArray(status.accounts)) return null
  const active = status.accounts.find((a) => a.active) ?? null
  const stale = typeof status.generated_at === 'number' && now - status.generated_at > STATUS_STALE_S
  if (!active) {
    return {
      email: status.active_email ?? null,
      foreign: Boolean(status.foreign_runtime),
      used: null,
      window: null,
      text: '?',
      countdown: null,
      severity: 'warning',
      stale
    }
  }
  const session = windowView(active.session)
  const weekly = windowView(active.weekly)
  const worse = [
    session && { name: 'session', ...session },
    weekly && { name: 'weekly', ...weekly }
  ]
    .filter(Boolean)
    .sort((a, b) => b.used - a.used)[0]
  const countdown = nextWall(status, worse, now)
  return {
    email: active.email,
    tier: active.tier || null,
    foreign: false,
    used: worse ? worse.used : null,
    window: worse ? worse.name : null,
    session,
    weekly,
    text: worse ? `${Math.round(worse.used)}%` : '–',
    countdown,
    severity: status.pause ? 'error' : usageSeverity(worse?.used),
    stale
  }
}

/** The first thing that will happen to the active account: a resume, a switch or a reset. */
function nextWall(status, worse, now) {
  if (status.pause) {
    const at = status.pause.resume_at
    return { kind: 'paused', at: at ?? null, text: at ? `resumes in ${formatDuration(at - now)}` : 'paused' }
  }
  const forecast = status.forecast || {}
  const switches = ['session', 'weekly']
    .map((name) => ({ name, at: forecast[name]?.switch_at }))
    .filter((f) => typeof f.at === 'number' && f.at > now)
    .sort((a, b) => a.at - b.at)
  if (switches.length) {
    const first = switches[0]
    return { kind: 'switch', window: first.name, at: first.at, text: `switch in ${formatDuration(first.at - now)}` }
  }
  if (worse?.resetsAt && worse.resetsAt > now) {
    return { kind: 'reset', window: worse.name, at: worse.resetsAt, text: `resets in ${formatDuration(worse.resetsAt - now)}` }
  }
  return null
}

export function accountsList(status) {
  if (!status || !Array.isArray(status.accounts)) return []
  return status.accounts.map((a) => ({
    email: a.email,
    tier: a.tier || null,
    active: Boolean(a.active),
    status: a.status || 'unknown',
    usable: a.usable !== false,
    note: a.note || '',
    queue: a.queue ?? null,
    session: windowView(a.session),
    weekly: windowView(a.weekly),
    lastResort: Boolean(a.last_resort)
  }))
}

export function memorySummary(guard, sched, now) {
  const snap = guard?.snapshot ?? null
  const pressure = snap?.pressure ?? null
  const stage = Number.isInteger(pressure?.stage) ? pressure.stage : 0
  const stale = !snap || (typeof snap.at === 'number' && now - snap.at > GUARD_STALE_S)
  const mem = sched?.memory ?? null
  return {
    stage,
    stageName: STAGE_NAMES[Math.min(stage, 3)],
    reasons: Array.isArray(pressure?.stage_reasons) ? pressure.stage_reasons : [],
    level: pressure?.level ?? 0,
    severity: stage >= 2 ? 'error' : stage >= 1 ? 'warning' : 'normal',
    swapUsed: pressure?.swap_used ?? null,
    budget: snap?.budget ?? null,
    devTotal: snap?.total ?? null,
    freeForAdmissionGb: mem?.free_for_admission_gb ?? null,
    brake: mem?.brake ?? null,
    stale
  }
}

// Format.reason in the app: the guard's reason code and numbers as a phrase
export function planReason(code, data = {}) {
  data = data || {}
  switch (code) {
    case 'bloated': return `grew to ${formatGb(data.size ?? 0)}`
    case 'bloated_unmanaged': return `at ${formatGb(data.size ?? 0)}, no terminal to restart it in`
    case 'duplicate': return data.keep != null ? `duplicate of :${data.keep}` : 'duplicate'
    case 'orphan': return 'its agent or terminal is gone'
    case 'idle': return `idle for ${data.minutes ?? 0} min`
    case 'budget': return `over the ${formatGb(data.budget ?? 0)} budget`
    case 'pressure': return data.level === 2 ? 'memory is critical' : 'memory is tight'
    case 'loop': return `restarted ${data.restarts ?? 0}× this hour, HMR loop`
    case 'loop_watched': return `keeps regrowing, ${data.restarts ?? 0} restarts this hour`
    case 'manual': return 'by hand'
    default: return ''
  }
}

export function planText(plan) {
  if (!plan) return null
  const why = planReason(plan.code, plan.data)
  if (plan.action === 'recycle') return `Restart when quiet · ${why}`
  if (plan.action === 'stop') return `Will stop · ${why}`
  return why ? why[0].toUpperCase() + why.slice(1) : null
}

function viewers(unit) {
  const clients = Array.isArray(unit.clients) ? unit.clients : []
  if (unit.attended) return 'you'
  if ((unit.tabs || []).length || clients.some((c) => c.kind === 'orca')) return 'agents'
  if (clients.some((c) => c.kind === 'headless')) return 'headless'
  return clients.length ? 'agents' : 'nobody'
}

function unitTitle(unit) {
  if ((unit.servers ?? 1) > 1) return 'Dev stack'
  const cwd = Array.isArray(unit.cwd) ? unit.cwd[0] : unit.cwd
  return (cwd || '').split('/').filter(Boolean).pop() || `pid ${unit.root}`
}

export function unitView(unit, plan) {
  const ports = Array.isArray(unit.ports) ? unit.ports : []
  const portLabel = ports.length ? (ports.length > 1 ? `:${ports[0]} +${ports.length - 1}` : `:${ports[0]}`) : `pid ${unit.root}`
  return {
    key: unit.key,
    title: unitTitle(unit),
    portLabel,
    ports,
    footprint: unit.footprint ?? 0,
    gb: formatGb(unit.footprint ?? 0),
    attended: Boolean(unit.attended),
    viewers: viewers(unit),
    agentWorking: Boolean(unit.agent_working),
    recyclable: Boolean(unit.recyclable),
    protected: Boolean(unit.protected),
    pin: unit.pin ?? null,
    terminal: unit.terminal ?? null,
    worktree: unit.worktree ?? null,
    plan: plan ? { action: plan.action, code: plan.code, text: planText(plan) } : null,
    // `claude-acc guard recycle|stop` take a port or the root pid, like the app's buttons
    target: ports.length ? `:${ports[0]}` : String(unit.root),
    pinTarget: unit.pin?.target ?? (ports.length ? `:${ports[0]}` : unit.launch_cwd || (unit.cwd || [])[0] || null)
  }
}

export function devUnits(guard) {
  const snap = guard?.snapshot
  if (!snap || !Array.isArray(snap.units)) return []
  const plans = new Map((snap.plans || []).map((p) => [p.unit, p]))
  return snap.units.map((u) => unitView(u, plans.get(u.key)))
}

/** Longest worktree path that contains `path`; worktrees nest (a child worktree inside its parent). */
export function worktreeFor(path, worktrees) {
  if (!path) return null
  let best = null
  for (const wt of worktrees) {
    if (!wt.path) continue
    if (path === wt.path || path.startsWith(wt.path.endsWith('/') ? wt.path : `${wt.path}/`)) {
      if (!best || wt.path.length > best.path.length) best = wt
    }
  }
  return best
}

function expandHome(path, home) {
  return path && home && path.startsWith('~') ? home + path.slice(1) : path
}

function schedJob(job, running, now) {
  const agent = job.agent || {}
  return {
    id: job.id,
    label: job.label,
    running,
    where: job.where ?? null,
    pane: agent.pane ?? null,
    agentName: agent.name ?? null,
    cwd: agent.worktree ?? null,
    paused: Boolean(job.paused),
    cancelled: Boolean(job.cancelled),
    memGb: running ? job.mem_now_gb ?? null : job.mem_predicted_gb ?? null,
    predictedGb: job.mem_predicted_gb ?? null,
    eta: running ? formatDuration(job.eta_s) : formatDuration(job.eta_start_s),
    reason: running ? null : job.reason?.text ?? null,
    position: job.position ?? null,
    waited: formatDuration(job.waited_s ?? (job.enqueued_at ? now - job.enqueued_at : null))
  }
}

export function schedJobs(sched, now) {
  if (!sched) return []
  return [
    ...(sched.running || []).map((j) => schedJob(j, true, now)),
    ...[...(sched.queue || [])].sort((a, b) => (a.position ?? 99) - (b.position ?? 99)).map((j) => schedJob(j, false, now))
  ]
}

/**
 * Dev servers and scheduler jobs per Orca worktree. `worktrees` come from Orca (path, id,
 * displayName); `panes` maps an Orca pane key to its worktree id, learned from agent events and
 * the terminal list. Whatever matches no worktree lands in `elsewhere`.
 */
export function groupByWorktree(units, jobs, worktrees, panes = new Map(), home = '') {
  const groups = new Map()
  const elsewhere = { units: [], jobs: [] }
  const slot = (wt) => {
    if (!groups.has(wt.id)) {
      groups.set(wt.id, { id: wt.id, path: wt.path, name: wt.displayName || wt.path.split('/').pop(), units: [], jobs: [] })
    }
    return groups.get(wt.id)
  }
  const byId = new Map(worktrees.map((w) => [w.id, w]))
  for (const unit of units) {
    const wt = worktreeFor(unit.worktree, worktrees)
    ;(wt ? slot(wt).units : elsewhere.units).push(unit)
  }
  for (const job of jobs) {
    const wt = (job.pane && byId.get(panes.get(job.pane))) || worktreeFor(expandHome(job.cwd, home), worktrees)
    ;(wt ? slot(wt).jobs : elsewhere.jobs).push(job)
  }
  return { worktrees: [...groups.values()], elsewhere }
}

function fresh(at, now, limit) {
  return typeof at === 'number' && now - at <= limit
}

export function healthSummary(files, now) {
  const ultra = files.perf?.ultra ?? null
  const hotspot = files.hotspot ?? null
  const fans = files.fans ?? null
  const janitor = files.janitor ?? null
  const updates = files.updates ?? null
  const awake = files.awake ?? null
  const lastSweep = janitor?.last_sweep ?? null
  return {
    ultra: ultra
      ? {
          on: Boolean(ultra.on),
          pendingManual: (ultra.pending_manual || []).length,
          pendingRoot: (ultra.pending_root || []).length,
          applied: (ultra.applied || []).length
        }
      : null,
    hotspot: hotspot
      ? {
          enabled: Boolean(hotspot.enabled),
          active: Boolean(hotspot.active) && fresh(hotspot.at, now, DAEMON_STALE_S),
          running: fresh(hotspot.at, now, DAEMON_STALE_S),
          via: hotspot.via ?? null,
          rateKbps: hotspot.rate_kbps ?? null,
          delayP50: hotspot.delay_p50_ms ?? null
        }
      : null,
    fans: fans
      ? {
          running: fresh(fans.at, now, DAEMON_STALE_S),
          mode: fans.mode ?? null,
          percent: fans.percent ?? null,
          cpu: fans.cpu ?? null,
          gpu: fans.gpu ?? null
        }
      : null,
    janitor: janitor
      ? {
          lastSweepAt: lastSweep?.at ?? null,
          freed: lastSweep?.freed ?? null,
          diskFree: janitor.disk_free ?? null,
          diskTotal: janitor.disk_total ?? null,
          alerts: (janitor.alerts || []).length,
          warnings: (janitor.warnings || []).length
        }
      : null,
    updates: updates
      ? {
          lastRunAt: updates.last_run?.at ?? null,
          ok: updates.last_run ? Boolean(updates.last_run.ok) : null,
          failed: updates.last_run?.failed ?? 0,
          updated: updates.last_run?.updated ?? 0,
          nextRun: updates.next_run ?? null
        }
      : null,
    awake: awakeSummary(awake, now)
  }
}

/** Stay Awake from the app's awake-state.json; `running` comes from the reader (the app's pid). */
export function awakeSummary(awake, now) {
  if (!awake) return null
  const running = awake.running !== false
  const on = running && Boolean(awake.on)
  let text = 'Off'
  if (on && awake.manual) {
    text = awake.forever || !awake.until ? 'Awake' : `Awake for ${formatDuration(awake.until - now)}`
  } else if (on && awake.hotspot) {
    text = 'Awake on hotspot'
  } else if (!running) {
    text = 'App not running'
  }
  return { on, manual: Boolean(awake.manual) && on, hotspot: Boolean(awake.hotspot) && on, until: awake.until ?? null, running, text }
}

/** Everything the panel and the status bar show, from the raw files. */
export function buildModel(files, context, now) {
  const units = devUnits(files.guard)
  const jobs = schedJobs(files.sched, now)
  const grouped = groupByWorktree(units, jobs, context.worktrees || [], context.panes, context.home)
  const status = files.status ?? null
  return {
    at: now,
    account: accountSummary(status, now),
    accounts: accountsList(status),
    switching: {
      pause: status?.pause ?? null,
      limitPause: Boolean(status?.limit_pause),
      drain: Boolean(status?.drain),
      orcaSelected: status?.orca_selected ?? null,
      thresholds: status?.thresholds ?? null
    },
    memory: memorySummary(files.guard, files.sched, now),
    worktrees: grouped.worktrees,
    elsewhere: grouped.elsewhere,
    jobs,
    activeWorktreeId: context.activeWorktreeId ?? null,
    health: healthSummary(files, now)
  }
}
