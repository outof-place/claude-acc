// Status bar items (Orca with plugin status bar support): the account ring, the memory brake and Stay Awake.

// ids are plugin ids (kebab-case); Orca keeps one plain line: no newlines, text ≤ 80, tooltip ≤ 512
export const ITEM_ACCOUNT = 'account'
export const ITEM_MEMORY = 'memory'
export const ITEM_AWAKE = 'awake'
const TEXT_MAX = 80
const TOOLTIP_MAX = 512

function clip(text, max) {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

function windowLine(name, window, now) {
  if (!window) return null
  const resets = window.resetsAt && window.resetsAt > now ? `, resets in ${formatIn(window.resetsAt - now)}` : ''
  return `${name}: ${Math.round(window.used)}% used${resets}`
}

function formatIn(seconds) {
  const m = Math.round(seconds / 60)
  return m < 60 ? `${m}m` : m < 1440 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m` : `${Math.floor(m / 1440)}d ${Math.floor((m % 1440) / 60)}h`
}

export function statusBarItems(model) {
  return rawItems(model).map((item) => ({
    ...item,
    text: clip(item.text, TEXT_MAX),
    tooltip: clip(item.tooltip.replace(/\s*\n\s*/g, ' · '), TOOLTIP_MAX)
  }))
}

function rawItems(model) {
  const now = model.at
  const account = model.account
  const items = []
  if (!account) {
    items.push({ id: ITEM_ACCOUNT, text: 'Claude –', tooltip: 'claude-acc has not written status.json yet', severity: 'warning', visible: true })
  } else {
    const countdown = account.countdown ? ` · ${account.countdown.text.replace(/ in /, ' ')}` : ''
    const tooltip = [
      account.foreign ? 'The runtime account is not one claude-acc manages' : `${account.email}${account.tier ? ` (${account.tier})` : ''}`,
      windowLine('Session', account.session, now),
      windowLine('Weekly', account.weekly, now),
      account.countdown?.text ? `Next: ${account.countdown.text}` : null,
      model.switching.pause ? 'Sessions are paused at the limit' : null,
      account.stale ? 'claude-acc has not ticked for over 10 minutes' : null
    ].filter(Boolean)
    items.push({
      id: ITEM_ACCOUNT,
      text: `Claude ${account.text}${countdown}`,
      tooltip: tooltip.join('\n'),
      severity: account.stale && account.severity === 'normal' ? 'warning' : account.severity,
      visible: true
    })
  }
  const memory = model.memory
  items.push({
    id: ITEM_MEMORY,
    text: `Memory ${memory.stageName}`,
    tooltip: [`Memory brake stage ${memory.stage} (${memory.stageName})`, ...memory.reasons].join('\n'),
    severity: memory.severity,
    visible: memory.stage >= 1
  })
  const awake = model.health.awake
  items.push({
    id: ITEM_AWAKE,
    text: awake?.text === 'Awake' || awake?.text === 'Awake on hotspot' ? 'Awake' : awake?.text ?? 'Awake',
    tooltip: 'Stay Awake (Claude Acc). Click to turn it off',
    severity: 'normal',
    visible: Boolean(awake?.on)
  })
  return items
}
