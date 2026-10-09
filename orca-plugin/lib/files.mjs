// Stat-polls claude-acc's state files like the menu bar app (Store.swift readLocal): the daemons
// replace them by rename, so inode, mtime and size tell a new version apart without reading it.

import { readFile, stat } from 'node:fs/promises'
import { homedir } from 'node:os'
import { join } from 'node:path'

export function stateDir(home = homedir()) {
  return join(home, '.local/share/claude-acc')
}

export const STATE_FILES = {
  status: 'status.json',
  guard: 'devguard-state.json',
  sched: 'sched/state.json',
  perf: 'perf-state.json',
  hotspot: 'hotspot-state.json',
  fans: 'fans-state.json',
  janitor: 'janitor-state.json',
  updates: 'updates-state.json',
  awake: 'awake-state.json'
}

function pidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    return error.code === 'EPERM'
  }
}

/** Trim what the model never reads, so a big file does not stay in memory twice. */
function slim(name, data) {
  if (name === 'perf') return { ultra: data?.ultra ?? null }
  if (name === 'fans' && data) return { ...data, history: undefined }
  if (name === 'guard' && data) return { snapshot: data.snapshot ?? null, events: (data.events || []).slice(-5) }
  if (name === 'awake' && data) return { ...data, running: pidAlive(data.pid) }
  return data
}

export class StatePoller {
  constructor(dir = stateDir()) {
    this.dir = dir
    this.seen = new Map()
    this.data = {}
  }

  /** Re-reads changed files; true when anything changed. */
  async poll() {
    let changed = false
    await Promise.all(
      Object.entries(STATE_FILES).map(async ([name, file]) => {
        const path = join(this.dir, file)
        let info
        try {
          info = await stat(path)
        } catch {
          if (this.seen.has(name)) {
            this.seen.delete(name)
            delete this.data[name]
            changed = true
          }
          return
        }
        const sig = `${info.ino}:${info.mtimeMs}:${info.size}`
        if (this.seen.get(name) === sig && name !== 'awake') return
        try {
          const next = slim(name, JSON.parse(await readFile(path, 'utf8')))
          const before = JSON.stringify(this.data[name] ?? null)
          this.seen.set(name, sig)
          if (JSON.stringify(next) !== before) {
            this.data[name] = next
            changed = true
          }
        } catch {
          // caught mid-write or broken: keep the last good copy and try again next round
        }
      })
    )
    return changed
  }
}
