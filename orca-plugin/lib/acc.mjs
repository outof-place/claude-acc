// Runs the `claude-acc` command the way the menu bar app does (CLI.swift): the installed shim,
// a PATH that finds `orca` and `claude`, and the last non-empty output line as the message.

import { execFile } from 'node:child_process'
import { access } from 'node:fs/promises'
import { homedir } from 'node:os'
import { join } from 'node:path'

export function accEnv(home = homedir()) {
  return {
    HOME: home,
    USER: process.env.USER || home.split('/').pop(),
    LANG: process.env.LANG || 'en_US.UTF-8',
    PATH: `${home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin`
  }
}

export async function accBinary(home = homedir()) {
  const path = join(home, '.local/bin/claude-acc')
  try {
    await access(path)
    return path
  } catch {
    return null
  }
}

export function lastLine(text) {
  const lines = String(text || '')
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
  const line = lines.at(-1) || ''
  return line.replace(/^(błąd|error): /, '')
}

/** Resolves { ok, code, stdout, stderr, message }; never rejects. */
export async function runAcc(argv, { home = homedir(), timeoutMs = 120_000, binary } = {}) {
  const bin = binary ?? (await accBinary(home))
  if (!bin) return { ok: false, code: -1, stdout: '', stderr: '', message: 'claude-acc is not installed (~/.local/bin/claude-acc)' }
  return new Promise((resolve) => {
    execFile(bin, argv, { env: accEnv(home), timeout: timeoutMs, maxBuffer: 4 * 1024 * 1024 }, (error, stdout, stderr) => {
      const code = error ? (typeof error.code === 'number' ? error.code : -1) : 0
      const message = lastLine(`${stdout}\n${stderr}`) || (error && !stdout && !stderr ? error.message : '')
      resolve({ ok: code === 0, code, stdout: String(stdout), stderr: String(stderr), message })
    })
  })
}
