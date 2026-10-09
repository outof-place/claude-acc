// Desktop notifications from state changes: limit approaching, account switched, sessions paused,
// memory brake. `sent` (persisted by the worker) keeps one notice per event across worker restarts.

const SWITCH_SOON_S = 10 * 60
const LIMIT_PERCENT = 90

/** Small snapshot of what notifications compare against, stored between worker runs. */
export function noticeBasis(model) {
  return {
    email: model.account?.email ?? null,
    stage: model.memory.stage,
    paused: Boolean(model.switching.pause),
    session: model.account?.session?.used ?? null,
    weekly: model.account?.weekly?.used ?? null
  }
}

export function notifications(prev, model, sent) {
  const out = []
  const now = model.at
  const once = (key, title, body) => {
    if (sent[key]) return
    sent[key] = now
    out.push({ key, title, body })
  }
  const account = model.account
  if (prev && account?.email && prev.email && prev.email !== account.email) {
    out.push({ key: `switched:${account.email}:${now}`, title: `Switched to ${account.email}`, body: `${prev.email} reached its limit; new sessions use ${account.email}` })
  }
  if (account?.email) {
    for (const name of ['session', 'weekly']) {
      const window = account[name]
      const before = prev && prev.email === account.email ? prev[name] : null
      if (window && window.used >= LIMIT_PERCENT && (before == null || before < LIMIT_PERCENT)) {
        once(`limit:${account.email}:${name}:${window.resetsAt}`, `Claude ${name} limit at ${Math.round(window.used)}%`, `${account.email}${account.countdown ? `: ${account.countdown.text}` : ''}`)
      }
    }
    const cd = account.countdown
    if (cd?.kind === 'switch' && cd.at - now <= SWITCH_SOON_S) {
      once(`switch-soon:${account.email}:${cd.window}:${Math.round(cd.at / 600)}`, 'Claude limit approaching', `${account.email} hits its ${cd.window} limit, ${cd.text}`)
    }
  }
  if (model.switching.pause && prev && !prev.paused) {
    const resume = model.account?.countdown?.kind === 'paused' ? `; ${model.account.countdown.text}` : ''
    out.push({ key: `paused:${now}`, title: 'Claude sessions paused', body: `Every account is at its limit${resume}` })
  }
  const stage = model.memory.stage
  if (prev && stage >= 2 && stage > prev.stage) {
    out.push({
      key: `brake:${stage}:${now}`,
      title: `Memory ${model.memory.stageName}`,
      body: model.memory.reasons.join('; ').slice(0, 900) || `Memory brake stage ${stage}`
    })
  }
  // forget dedupe keys after a day
  for (const [key, at] of Object.entries(sent)) if (now - at > 86400) delete sent[key]
  return out
}
