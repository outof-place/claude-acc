// claude-acc in Orca: status bar, live panel, Cmd-J commands, worktree card lines and notices,
// all from the state files claude-acc's daemons write (no `claude-acc status --json`: it takes a
// lock and reads the Keychain). Orca builds without status bar items or live panel messaging get
// everything else: the features are detected at runtime. The data itself comes from lib/core.mjs;
// this file is only the Orca plugin side (host API, runtime RPC, panel transport).

import { createAccCore, cardLine, mergeComment, noticeBasis, notifications, statusBarItems } from './lib/core.mjs'
import { OrcaRpc } from './lib/orca-rpc.mjs'

export const PANEL_ID = 'claude-acc'
const POLL_MS = 2000
const ORCA_REFRESH_MS = 30_000
// countdowns move by the minute even when no file changes
const REPUBLISH_MS = 30_000
const CARD_MIN_INTERVAL_MS = 60_000
// Orca reaps a worker after 5 idle minutes; a host call now and then keeps the monitor running
const KEEPALIVE_MS = 60_000
const STORAGE_KEY = 'state'

let current = null

export default async function activate(orca) {
  current = new AccPlugin(orca)
  current.register()
  current.start()
}

export async function deactivate() {
  current?.stop()
  current = null
}

export class AccPlugin {
  constructor(orca, options = {}) {
    this.orca = orca
    this.core = options.core ?? createAccCore(options)
    this.rpc = options.rpc ?? new OrcaRpc()
    this.context = { worktrees: [], panes: new Map(), activeWorktreeId: null }
    this.model = null
    this.timers = []
    this.lastHostCall = 0
    this.lastOrcaRefresh = 0
    this.lastPublish = 0
    this.sentItems = new Map()
    this.cardWrites = new Map()
    this.store = { basis: null, sent: {}, prefs: { cards: true, notifications: true } }
    this.storeLoaded = false
    this.logged = new Set()
    this.busy = false
    this.panelReady = false
  }

  // ---------- host features ----------

  async hostCall(method, params) {
    this.lastHostCall = Date.now()
    return this.orca.host.call(method, params)
  }

  get statusBar() {
    const bar = this.orca.statusBar
    return bar && typeof bar.update === 'function' ? bar : null
  }

  get panels() {
    const panels = this.orca.panels
    return panels && typeof panels.postMessage === 'function' ? panels : null
  }

  logOnce(key, message) {
    if (this.logged.has(key)) return
    this.logged.add(key)
    this.orca.log(message)
  }

  // ---------- lifecycle ----------

  register() {
    const commands = {
      'claude-acc.status': () => this.showStatus(),
      'claude-acc.switch-next': () => this.act('switch', { email: 'auto' }),
      'claude-acc.resume': () => this.act('resume'),
      'claude-acc.pause-toggle': () => this.act('pause', { on: !this.model?.switching.limitPause }),
      'claude-acc.drain-toggle': () => this.act('drain', { on: !this.model?.switching.drain }),
      'claude-acc.restart-dev-server': () => this.devServers('recycle'),
      'claude-acc.stop-dev-server': () => this.devServers('stop'),
      'claude-acc.cancel-builds': () => this.cancelWorktreeBuilds(),
      'claude-acc.clean': () => this.act('clean'),
      'claude-acc.update': () => this.act('update'),
      'claude-acc.ultra-toggle': () => this.act('ultra', { on: !this.model?.health.ultra?.on }),
      'claude-acc.dictate': () => this.act('dictate'),
      'claude-acc.awake-toggle': () => this.act('awake', { toggle: true }),
      // the settings are native: Pod Menu's panel, at its claude-acc services section
      'claude-acc.settings': () => this.act('panel', { section: 'services' }, { quiet: true })
    }
    for (const [id, handler] of Object.entries(commands)) this.orca.commands.register(id, handler)
    this.orca.events.on('agent.status.changed', (payload) => {
      if (payload?.paneKey && payload.worktreeId) this.context.panes.set(payload.paneKey, payload.worktreeId)
    })
    const refreshSoon = () => {
      this.lastOrcaRefresh = 0
    }
    this.orca.events.on('worktree.created', refreshSoon)
    this.orca.events.on('worktree.removed', refreshSoon)
    const panels = this.panels
    if (panels && typeof panels.onMessage === 'function') {
      panels.onMessage(PANEL_ID, (message) => this.onPanelMessage(message))
    }
  }

  start() {
    const loop = () => {
      void this.tick().catch((error) => this.logOnce(`tick:${error.message}`, `tick failed: ${error.stack ?? error}`))
    }
    loop()
    this.timers.push(setInterval(loop, POLL_MS))
    this.timers.push(
      setInterval(() => {
        if (Date.now() - this.lastHostCall >= KEEPALIVE_MS) void this.hostCall('storage.keys').catch(() => {})
      }, KEEPALIVE_MS)
    )
  }

  stop() {
    for (const timer of this.timers) clearInterval(timer)
    this.timers = []
  }

  async loadStore() {
    if (this.storeLoaded) return
    this.storeLoaded = true
    try {
      const { value } = await this.hostCall('storage.get', { key: STORAGE_KEY })
      if (value && typeof value === 'object') {
        this.store = { ...this.store, ...value, prefs: { ...this.store.prefs, ...(value.prefs || {}) } }
      }
    } catch (error) {
      this.logOnce('storage', `plugin storage unavailable: ${error.message}`)
    }
  }

  async saveStore() {
    try {
      await this.hostCall('storage.set', { key: STORAGE_KEY, value: this.store })
    } catch (error) {
      this.logOnce('storage-set', `could not save plugin storage: ${error.message}`)
    }
  }

  // ---------- the loop ----------

  async tick() {
    if (this.busy) return
    this.busy = true
    try {
      await this.loadStore()
      let changed = await this.core.poll()
      if (Date.now() - this.lastOrcaRefresh >= ORCA_REFRESH_MS) {
        changed = (await this.refreshOrca()) || changed
      }
      if (changed || !this.model || Date.now() - this.lastPublish >= REPUBLISH_MS) {
        this.model = this.core.model(this.context)
        this.lastPublish = Date.now()
        await this.publish(this.model)
      }
    } finally {
      this.busy = false
    }
  }

  /** Worktrees (path, id, comment) and the pane → worktree map from Orca's terminal list. */
  async refreshOrca() {
    this.lastOrcaRefresh = Date.now()
    try {
      const listed = await this.rpc.call('worktree.list', { limit: 1000 })
      const worktrees = (listed?.worktrees || [])
        .filter((w) => w && w.path && (w.hostId == null || w.hostId === 'local'))
        .map((w) => ({ id: w.id, path: w.path, displayName: w.displayName || '', branch: w.branch || '', comment: w.comment ?? '' }))
      const terminals = await this.rpc.call('terminal.list', { includeVisualLayouts: false })
      const panes = new Map(this.context.panes)
      this.terminals = terminals?.terminals || []
      for (const t of this.terminals) {
        if (t.tabId && t.leafId && t.worktreeId) panes.set(`${t.tabId}:${t.leafId}`, t.worktreeId)
      }
      const before = JSON.stringify(this.context.worktrees.map((w) => [w.id, w.path, w.displayName]))
      this.context.worktrees = worktrees
      this.context.panes = panes
      return before !== JSON.stringify(worktrees.map((w) => [w.id, w.path, w.displayName]))
    } catch (error) {
      this.logOnce(`rpc:${error.code}`, `Orca runtime RPC unavailable (${error.code}): ${error.message}; worktree grouping and cards are off until it answers`)
      return false
    }
  }

  async publish(model) {
    await this.publishStatusBar(model)
    this.postToPanel({ type: 'model', model, prefs: this.store.prefs, features: this.features() })
    await this.notify(model)
    if (this.store.prefs.cards) await this.updateCards(model)
  }

  features() {
    return { statusBar: Boolean(this.statusBar), livePanel: Boolean(this.panels), rpc: this.context.worktrees.length > 0 }
  }

  async publishStatusBar(model) {
    const bar = this.statusBar
    if (!bar) return
    for (const item of statusBarItems(model)) {
      const { id, ...patch } = item
      const text = JSON.stringify(patch)
      if (this.sentItems.get(id) === text) continue
      try {
        await bar.update(id, patch)
        this.lastHostCall = Date.now()
        this.sentItems.set(id, text)
      } catch (error) {
        this.logOnce(`statusbar:${id}`, `status bar ${id}: ${error.message}`)
      }
    }
  }

  postToPanel(message) {
    const panels = this.panels
    if (!panels) return
    try {
      const sent = panels.postMessage(PANEL_ID, message)
      if (sent && typeof sent.catch === 'function') sent.catch((error) => this.logOnce('panel-post', `panel message: ${error.message}`))
    } catch (error) {
      this.logOnce('panel-post', `panel message: ${error.message}`)
    }
  }

  async notify(model) {
    const prev = this.store.basis
    const notices = notifications(prev, model, this.store.sent)
    this.store.basis = noticeBasis(model)
    const changed = JSON.stringify(prev) !== JSON.stringify(this.store.basis) || notices.length
    if (this.store.prefs.notifications) {
      for (const notice of notices) await this.show(notice.title, notice.body)
    }
    if (changed) await this.saveStore()
  }

  async show(title, body) {
    try {
      await this.hostCall('notifications.show', { title: title.slice(0, 120), body: body ? String(body).slice(0, 1000) : undefined })
    } catch (error) {
      this.logOnce(`notify:${error.code}`, `notification: ${error.message}`)
    }
  }

  async updateCards(model) {
    const groups = new Map(model.worktrees.map((g) => [g.id, g]))
    const due = (wt) => {
      const line = cardLine(groups.get(wt.id) ?? { units: [], jobs: [] })
      const next = mergeComment(wt.comment, line)
      // removing our line goes at once; a changed line waits a minute after the previous write
      if (next === null || (line && Date.now() - (this.cardWrites.get(wt.id) ?? 0) < CARD_MIN_INTERVAL_MS)) return null
      return next
    }
    if (!this.context.worktrees.some(due)) return
    // the cached comments can be 30 s old: read them again, so a comment typed meanwhile is seen
    await this.refreshOrca()
    for (const wt of this.context.worktrees) {
      const next = due(wt)
      if (next === null) continue
      try {
        await this.rpc.call('worktree.set', { worktree: `id:${wt.id}`, comment: next })
        wt.comment = next
        this.cardWrites.set(wt.id, Date.now())
      } catch (error) {
        this.logOnce(`card:${error.code}`, `worktree card ${wt.path}: ${error.message}`)
      }
    }
  }

  // ---------- actions ----------

  /** Runs one action and reports the outcome as a notification (Cmd-J shows no result). */
  async act(action, args = {}, { quiet = false } = {}) {
    const { argv, long } = this.core.resolve(action, args)
    const job = this.core.act(action, args).then(async (result) => {
      if (!quiet || !result.ok) await this.show(result.ok ? `claude-acc ${argv[0]}` : `claude-acc ${argv[0]} failed`, result.message || (result.ok ? 'done' : `exit ${result.code}`))
      this.lastPublish = 0
      void this.tick()
      return result
    })
    if (long) {
      await this.show(`claude-acc ${argv.join(' ')}`, 'Started; a notification follows when it finishes')
      return { started: true }
    }
    const result = await job
    return { ok: result.ok, message: result.message }
  }

  /** The focused worktree: its terminals from the plugin API, their paths from the runtime list. */
  async activeWorktree() {
    let context = null
    try {
      context = await this.hostCall('workspace.readContext')
    } catch (error) {
      this.logOnce('readContext', `workspace.readContext: ${error.message}`)
    }
    if (!context) return null
    if (!this.context.worktrees.length || !this.terminals) await this.refreshOrca()
    const ids = new Set((context.terminals || []).map((t) => t.id))
    let terminal = (this.terminals || []).find((t) => ids.has(t.handle))
    if (!terminal && ids.size) {
      await this.refreshOrca()
      terminal = (this.terminals || []).find((t) => ids.has(t.handle))
    }
    const wt =
      (terminal && this.context.worktrees.find((w) => w.id === terminal.worktreeId || w.path === terminal.worktreePath)) ||
      this.context.worktrees.find(
        (w) => w.displayName === context.displayName && w.branch.replace(/^refs\/heads\//, '') === String(context.branch).replace(/^refs\/heads\//, '')
      )
    return wt ? { id: wt.id, path: wt.path, name: wt.displayName || context.displayName } : { id: null, path: null, name: context.displayName }
  }

  async groupForActive() {
    const wt = await this.activeWorktree()
    if (!wt) return { wt: null, group: null }
    this.model = this.core.model(this.context)
    return { wt, group: this.model.worktrees.find((g) => g.id === wt.id) ?? null }
  }

  async devServers(verb) {
    const { wt, group } = await this.groupForActive()
    if (!wt) return this.show('No worktree in focus', 'Open a worktree, then run the command again')
    const units = (group?.units || []).filter((u) => verb !== 'recycle' || u.recyclable)
    if (!units.length) return this.show(`No dev server in ${wt.name}`, verb === 'recycle' ? 'Nothing the guard can restart runs in this worktree' : 'Nothing to stop')
    const results = []
    for (const unit of units) {
      const result = await this.core.act('guard', { verb, target: unit.target })
      results.push(`${unit.title} ${unit.portLabel}: ${result.ok ? (verb === 'recycle' ? 'restarted' : 'stopped') : result.message || 'failed'}`)
    }
    await this.show(verb === 'recycle' ? `Restart in ${wt.name}` : `Stop in ${wt.name}`, results.join('\n'))
    this.lastPublish = 0
    return { ok: true }
  }

  async cancelWorktreeBuilds() {
    const { wt, group } = await this.groupForActive()
    if (!wt) return this.show('No worktree in focus', 'Open a worktree, then run the command again')
    const jobs = group?.jobs || []
    if (!jobs.length) return this.show(`No builds in ${wt.name}`, 'The scheduler has nothing running or queued for this worktree')
    const lines = []
    for (const job of jobs) {
      const result = await this.core.act('cancel', { target: job.id })
      let outcome = result.message
      try {
        outcome = JSON.parse(result.stdout).jobs.map((j) => j.result).join(', ') || 'not found'
      } catch {}
      lines.push(`${job.label}: ${outcome}`)
    }
    await this.show(`Builds in ${wt.name}`, lines.join('\n'))
    return { ok: true }
  }

  async showStatus() {
    if (!this.model) await this.tick()
    const m = this.model
    const a = m?.account
    const lines = [
      a ? `${a.email ?? 'unknown account'}: ${a.text}${a.countdown ? `, ${a.countdown.text}` : ''}` : 'No account data',
      m ? `Memory: ${m.memory.stageName}` : null,
      m ? `Dev servers: ${m.worktrees.reduce((n, g) => n + g.units.length, 0) + m.elsewhere.units.length}` : null,
      m ? `Builds: ${m.jobs.filter((j) => j.running).length} running, ${m.jobs.filter((j) => !j.running).length} queued` : null
    ].filter(Boolean)
    await this.show('Claude Acc', lines.join('\n'))
    return { ok: true }
  }

  async onPanelMessage(message) {
    if (!message || typeof message !== 'object') return
    if (message.type === 'hello') {
      this.panelReady = true
      if (!this.model) await this.tick()
      this.postToPanel({ type: 'model', model: this.model, prefs: this.store.prefs, features: this.features() })
      return
    }
    if (message.type === 'prefs' && message.prefs && typeof message.prefs === 'object') {
      for (const key of ['cards', 'notifications']) {
        if (typeof message.prefs[key] === 'boolean') this.store.prefs[key] = message.prefs[key]
      }
      await this.saveStore()
      this.postToPanel({ type: 'model', model: this.model, prefs: this.store.prefs, features: this.features() })
      return
    }
    if (message.type === 'action') {
      const id = typeof message.id === 'string' ? message.id.slice(0, 64) : null
      try {
        const { long } = this.core.resolve(String(message.action), message.args)
        if (long) {
          this.postToPanel({ type: 'result', id, ok: true, message: 'Started; a notification follows when it finishes' })
          await this.act(String(message.action), message.args)
          return
        }
        const result = await this.core.act(String(message.action), message.args)
        this.postToPanel({ type: 'result', id, ok: result.ok, message: result.message })
        this.lastPublish = 0
        void this.tick()
      } catch (error) {
        this.postToPanel({ type: 'result', id, ok: false, message: error.message })
      }
    }
  }
}
