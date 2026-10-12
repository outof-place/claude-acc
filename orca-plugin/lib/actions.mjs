// Panel and command actions as `claude-acc` argv: a closed list with checked arguments, run with
// execFile (no shell), so nothing a panel sends can become a different command.

const PORT_OR_PID = /^(?::\d{1,5}|\d{1,7})$/
const JOB_ID = /^j-\d+-[0-9a-f]{4}$/
const PANE_KEY = /^[A-Za-z0-9_-]{1,128}:[0-9a-f-]{36}$/
const EMAIL = /^[^\s@]{1,128}@[^\s@]{1,253}$/

function absolutePath(value) {
  return typeof value === 'string' && value.startsWith('/') && !value.includes('\0') && value.length < 4096
}

function onOff(value) {
  return value ? 'on' : 'off'
}

/** Returns { argv, long } for an action, or throws for anything not on the list. */
export function actionArgv(action, args = {}) {
  args = args && typeof args === 'object' ? args : {}
  switch (action) {
    case 'switch':
      if (args.email === 'auto') return { argv: ['switch', '--auto'] }
      if (typeof args.email !== 'string' || !EMAIL.test(args.email)) throw new Error('switch: bad email')
      return { argv: ['switch', args.email] }
    case 'resume':
      return { argv: ['resume'] }
    case 'pause':
      return { argv: ['pause', onOff(args.on)] }
    case 'drain':
      return { argv: ['drain', onOff(args.on)] }
    case 'guard': {
      const verb = args.verb
      if (verb === 'recycle' || verb === 'stop') {
        if (!PORT_OR_PID.test(String(args.target))) throw new Error(`guard ${verb}: bad target`)
        return { argv: ['guard', verb, String(args.target)] }
      }
      if (verb === 'pin' || verb === 'unpin') {
        const target = String(args.target ?? '')
        if (!PORT_OR_PID.test(target) && !absolutePath(target)) throw new Error(`guard ${verb}: bad target`)
        return { argv: ['guard', verb, target] }
      }
      throw new Error('guard: unknown verb')
    }
    case 'cancel': {
      const target = String(args.target ?? '')
      if (!JOB_ID.test(target) && !PANE_KEY.test(target)) throw new Error('cancel: bad job id or pane')
      return { argv: ['sched', 'cancel', target, ...(args.queuedOnly ? ['--queued'] : []), '--json'] }
    }
    case 'clean':
      return { argv: ['clean'], long: true }
    case 'update':
      return { argv: ['update'], long: true }
    case 'ultra':
      return { argv: ['perf', 'ultra', onOff(args.on)], long: true }
    case 'hotspot':
      return { argv: ['hotspot', onOff(args.on)] }
    case 'dictate':
      return { argv: ['dictate', 'toggle'] }
    case 'panel':
      // `claude-acc panel [section]` opens the menu helper's panel (claude-acc://panel/<section>)
      if (args.section == null) return { argv: ['panel'] }
      if (args.section !== 'services') throw new Error('panel: unknown section')
      return { argv: ['panel', 'services'] }
    case 'awake':
      if (args.toggle) return { argv: ['awake', 'toggle'] }
      if (args.on && args.for != null) {
        if (!/^\d{1,7}$/.test(String(args.for))) throw new Error('awake: bad duration')
        return { argv: ['awake', 'on', '--for', String(args.for)] }
      }
      return { argv: ['awake', onOff(args.on)] }
    default:
      throw new Error(`unknown action ${action}`)
  }
}
