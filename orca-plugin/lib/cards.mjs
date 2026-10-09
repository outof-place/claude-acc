// Worktree card comments: one line of ours, never over a comment someone else wrote. devguard
// (devguard_core.note) writes "devguard: …" notes the same way, so its line stays in front.

export const CARD_PREFIX = 'claude-acc:'
const MACHINE_PREFIXES = ['devguard', CARD_PREFIX]

/** Half-gigabyte steps, so a growing server does not rewrite the card every few seconds. */
function roughGb(bytes) {
  return `${(Math.round((bytes / 1024 ** 3) * 2) / 2).toFixed(1)} GB`
}

/** Our line for one worktree group, or null when it has nothing to show. */
export function cardLine(group) {
  const parts = []
  const units = [...(group.units || [])].sort((a, b) => b.footprint - a.footprint)
  for (const unit of units.slice(0, 2)) {
    let text = `${unit.portLabel} ${roughGb(unit.footprint)}`
    if (unit.attended) text += ' watched'
    if (unit.pin) text += ' pinned'
    if (unit.plan?.action === 'recycle') text += ', restart planned'
    if (unit.plan?.action === 'stop') text += ', stop planned'
    parts.push(text)
  }
  if (units.length > 2) parts.push(`+${units.length - 2} more`)
  const jobs = group.jobs || []
  const running = jobs.filter((j) => j.running).length
  const queued = jobs.length - running
  if (running) parts.push(`${running} build${running > 1 ? 's' : ''} running`)
  if (queued) parts.push(`${queued} queued`)
  return parts.length ? `${CARD_PREFIX} ${parts.join(' · ')}` : null
}

/**
 * The comment to write, or null to leave the card alone: no change, or a line someone else
 * wrote (the user's or an agent's), which we never touch.
 */
export function mergeComment(current, line) {
  const lines = String(current ?? '').split('\n')
  const foreign = lines.filter((l) => l.trim() && !MACHINE_PREFIXES.some((p) => l.startsWith(p)))
  if (foreign.length) return null
  const machine = lines.filter((l) => l.startsWith('devguard'))
  const next = [...machine, ...(line ? [line] : [])].join('\n')
  return next === String(current ?? '') ? null : next
}
