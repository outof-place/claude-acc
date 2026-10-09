// lib/core.mjs on its own, the way a host other than the Orca plugin would use it.
import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { STATE_FILES, createAccCore, statusBarItems } from '../lib/core.mjs'
import { NOW, WORKTREES, guard, status } from './fixtures.mjs'

test('core reads the state files under HOME and builds the model for a host context', async () => {
  const home = mkdtempSync(join(tmpdir(), 'acc-core-'))
  const dir = join(home, '.local/share/claude-acc')
  mkdirSync(dir, { recursive: true })
  writeFileSync(join(dir, STATE_FILES.status), JSON.stringify(status()))
  writeFileSync(join(dir, STATE_FILES.guard), JSON.stringify(guard()))
  const runs = []
  const core = createAccCore({ home, now: () => NOW, run: async (argv, opts) => (runs.push([argv, opts]), { ok: true, code: 0, message: 'ok' }) })
  assert.equal(await core.poll(), true)
  assert.equal(await core.poll(), false)
  const model = core.model({ worktrees: WORKTREES })
  assert.equal(model.account.text, '84%')
  assert.equal(model.worktrees[0].units[0].target, ':3000')
  assert.equal(statusBarItems(model)[0].text, 'Claude 84% · switch 14m')
  const result = await core.act('clean')
  assert.deepEqual([result.ok, result.long, result.argv], [true, true, ['clean']])
  assert.equal(runs[0][1].timeoutMs, 30 * 60_000)
  await assert.rejects(core.act('guard', { verb: 'stop', target: 'x; y' }))
  rmSync(home, { recursive: true, force: true })
})
