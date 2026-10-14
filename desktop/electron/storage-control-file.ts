import { mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { randomUUID } from 'node:crypto'
import type { StorageRunControls } from './storage-cleanup.js'

/** A cooperative mailbox owned by exactly this maintenance operation. No
 * recursive cleanup and no control of arbitrary or stale operation paths. */
export async function withStorageControlFile<T>(directory: string, controls: StorageRunControls, run: (payload: { controlFile: string; operationId: string; ownerPid: number; ownerNonce: string }) => Promise<T>): Promise<T> {
  if (!/^[a-zA-Z0-9-]{1,80}$/.test(controls.operationId)) throw new Error('Invalid storage control identity')
  const root = resolve(directory)
  const path = join(root, controls.operationId + '.json')
  if (dirname(path) !== root) throw new Error('Invalid storage control path')
  mkdirSync(root, { recursive: true })
  const nonce = randomUUID()
  const value = (state: 'run' | 'pause' | 'cancel') => ({ operationId: controls.operationId, ownerPid: process.pid, ownerNonce: nonce, state })
  writeFileSync(path, JSON.stringify(value(controls.mode())), { encoding: 'utf8', flag: 'wx' })
  let unbind: (() => void) | undefined
  try {
    unbind = controls.bind(mode => {
      const previous = JSON.parse(readFileSync(path, 'utf8'))
      if (previous.ownerNonce !== nonce) throw new Error('Storage control owner changed')
      const temporary = path + '.' + randomUUID() + '.tmp'
      try { writeFileSync(temporary, JSON.stringify(value(mode)), { encoding: 'utf8', flag: 'wx' }); renameSync(temporary, path) }
      finally { try { unlinkSync(temporary) } catch { /* already published */ } }
    })
    return await run({ controlFile: path, operationId: controls.operationId, ownerPid: process.pid, ownerNonce: nonce })
  } finally {
    unbind?.()
    try { if (JSON.parse(readFileSync(path, 'utf8')).ownerNonce === nonce) unlinkSync(path) } catch { /* never remove an unknown control file */ }
  }
}
