// claude-acc's data layer without any host: reads the daemons' state files, builds the view model,
// runs `claude-acc` actions from the allowlist. No Orca or plugin API in here, so the same module
// serves the Orca plugin worker (worker.mjs) and can be imported as-is by a host that embeds
// claude-acc in its own main process (Pod). Everything host-specific (worktree list, terminals,
// card writes, status bar, panel transport, notifications) stays with the caller.

import { homedir } from 'node:os'
import { actionArgv } from './actions.mjs'
import { runAcc } from './acc.mjs'
import { cardLine, mergeComment, CARD_PREFIX } from './cards.mjs'
import { StatePoller, STATE_FILES, stateDir } from './files.mjs'
import { buildModel } from './model.mjs'
import { noticeBasis, notifications } from './notify.mjs'
import { statusBarItems } from './statusbar.mjs'

export const LONG_ACTION_TIMEOUT_MS = 30 * 60_000
export const ACTION_TIMEOUT_MS = 120_000

/**
 * @param {{ home?: string, poller?: StatePoller, run?: (argv: string[], opts?: object) => Promise<object>, now?: () => number }} [options]
 */
export function createAccCore(options = {}) {
  const home = options.home ?? homedir()
  const poller = options.poller ?? new StatePoller(stateDir(home))
  const run = options.run ?? ((argv, opts) => runAcc(argv, { home, ...opts }))
  const now = options.now ?? (() => Date.now() / 1000)
  return {
    home,
    /** Re-reads the state files that changed; true when anything did. */
    poll: () => poller.poll(),
    /** The parsed files by name (status, guard, sched, perf, hotspot, fans, janitor, updates, awake). */
    get files() {
      return poller.data
    },
    stateDir: poller.dir,
    /**
     * The view model. `context` comes from the host: `worktrees` [{id, path, displayName}],
     * `panes` Map(paneKey → worktreeId), `activeWorktreeId`.
     */
    model: (context = {}) => buildModel(poller.data, { home, worktrees: [], panes: new Map(), ...context }, now()),
    /** Validates and resolves an action; throws for anything not on the allowlist. */
    resolve: (action, args) => actionArgv(action, args),
    /** Runs an action; resolves { ok, code, stdout, stderr, message, long }, never rejects. */
    async act(action, args) {
      const { argv, long } = actionArgv(action, args)
      const result = await run(argv, { timeoutMs: long ? LONG_ACTION_TIMEOUT_MS : ACTION_TIMEOUT_MS })
      return { ...result, long: Boolean(long), argv }
    },
    now
  }
}

export { CARD_PREFIX, STATE_FILES, cardLine, mergeComment, noticeBasis, notifications, statusBarItems }
