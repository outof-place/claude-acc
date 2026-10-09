// Orca's owner RPC over the runtime unix socket, the same transport the `orca` CLI uses
// (src/cli/runtime/transport.ts): one NDJSON request per connection, keepalive frames skipped.
// The plugin API has no worktree comments or terminal lists, so the worker reads them here.

import { execFile } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { createConnection } from 'node:net'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'

const METADATA = 'orca-runtime.json'
// createRuntimeTransportMetadata in Orca: <userData>/o-<pid>-<suffix>.sock
const SOCKET_RE = /\/o-(\d+)-[A-Za-z0-9_-]+\.sock$/

async function readMetadata(dir) {
  try {
    const meta = JSON.parse(await readFile(join(dir, METADATA), 'utf8'))
    return meta && typeof meta === 'object' ? { dir, meta } : null
  } catch {
    return null
  }
}

/** Unix socket paths the Orca main process listens on, from lsof (the worker's parent). */
function parentSockets(pid) {
  return new Promise((resolve) => {
    execFile('/usr/sbin/lsof', ['-a', '-p', String(pid), '-U', '-F', 'n'], { timeout: 5000 }, (error, stdout) => {
      if (error && !stdout) return resolve([])
      resolve(
        String(stdout)
          .split('\n')
          .filter((line) => line.startsWith('n/'))
          .map((line) => line.slice(1))
      )
    })
  })
}

/**
 * Metadata of the Orca instance that forked this worker: its pid is our parent, and its socket
 * sits in its own userData folder, so an isolated or dev instance is found as well as the main one.
 */
export async function findRuntime({ ppid = process.ppid, home = homedir(), userData = null } = {}) {
  const support = join(home, 'Library/Application Support')
  const candidates = [userData, join(support, 'orca'), join(support, 'Orca'), join(support, 'orca-dev')].filter(Boolean)
  for (const dir of candidates) {
    const found = await readMetadata(dir)
    if (found && (userData === dir || found.meta.pid === ppid)) return found
  }
  for (const path of await parentSockets(ppid)) {
    const m = SOCKET_RE.exec(path)
    if (m && Number(m[1]) === ppid) {
      const found = await readMetadata(dirname(path))
      if (found) return found
    }
  }
  return null
}

function unixEndpoint(meta) {
  const transports = Array.isArray(meta.transports) ? meta.transports : meta.transport ? [meta.transport] : []
  return transports.find((t) => t && t.kind === 'unix')?.endpoint ?? null
}

export class RpcError extends Error {
  constructor(code, message) {
    super(message)
    this.code = code
  }
}

/** One request; resolves with `result`, rejects with RpcError. */
export function rpcRequest(meta, method, params, timeoutMs = 10_000) {
  const endpoint = unixEndpoint(meta)
  if (!endpoint) return Promise.reject(new RpcError('runtime_unavailable', 'no unix transport in Orca runtime metadata'))
  return new Promise((resolve, reject) => {
    const id = randomUUID()
    const socket = createConnection(endpoint)
    let buffer = ''
    let settled = false
    const finish = (error, value) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      socket.destroy()
      error ? reject(error) : resolve(value)
    }
    const timer = setTimeout(() => finish(new RpcError('runtime_timeout', `${method} timed out`)), timeoutMs)
    socket.setEncoding('utf8')
    socket.on('error', (error) => finish(new RpcError('runtime_unavailable', error.message)))
    socket.on('close', () => finish(new RpcError('runtime_unavailable', 'Orca closed the connection')))
    socket.on('data', (chunk) => {
      buffer += chunk
      let newline
      while ((newline = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, newline).trim()
        buffer = buffer.slice(newline + 1)
        if (!line) continue
        let frame
        try {
          frame = JSON.parse(line)
        } catch {
          return finish(new RpcError('invalid_runtime_response', 'invalid frame'))
        }
        if (frame && frame._keepalive) {
          timer.refresh()
          continue
        }
        if (frame.id !== id) return finish(new RpcError('invalid_runtime_response', 'mismatched response id'))
        if (frame.ok) return finish(null, frame.result)
        return finish(new RpcError(frame.error?.code ?? 'error', frame.error?.message ?? `${method} failed`))
      }
    })
    socket.on('connect', () => {
      socket.write(`${JSON.stringify({ id, authToken: meta.authToken, method, params })}\n`)
    })
  })
}

/** Lazily located runtime with one retry after Orca restarts (new pid, socket and token). */
export class OrcaRpc {
  constructor(options = {}) {
    this.options = options
    this.runtime = null
  }

  async call(method, params = {}, timeoutMs) {
    for (let attempt = 0; attempt < 2; attempt++) {
      if (!this.runtime) this.runtime = await findRuntime(this.options)
      if (!this.runtime) throw new RpcError('runtime_unavailable', 'Orca runtime metadata not found')
      try {
        return await rpcRequest(this.runtime.meta, method, params, timeoutMs)
      } catch (error) {
        if (error.code !== 'runtime_unavailable' || attempt === 1) throw error
        this.runtime = null
      }
    }
    throw new RpcError('runtime_unavailable', 'unreachable')
  }
}
