import { existsSync, mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'
import { auditContextDifference, type AuditContext } from './project-audit-context.js'

export type AuditLifecycle = { schemaVersion: 1; operationId: string; phase: 'running' | 'done' | 'error' | 'interrupted'; context: AuditContext; evidenceContext?: AuditContext; ownerPid: number; ownerNonce: string; startedAt: number; finishedAt?: number; error?: string }
import type { ProjectAuditStatus } from './project-audit-types.js'
export type { ProjectAuditStatus } from './project-audit-types.js'
export type AuditJob = { identity: string; promise: Promise<void>; finishedAt?: number; error?: string; resource: string }
export type AuditRequest = { runDir: string; workspaceId: string; projectName: string; workspacePath: string; inputs: Record<string, unknown>; context: AuditContext; currentContext: () => AuditContext }
const statePath = (directory: string) => join(directory, 'current-audit-state.json')
export function auditJson(path: string): Record<string, any> | undefined { try { return JSON.parse(readFileSync(path, 'utf8').replace(/^\uFEFF/, '')) } catch { return } }
function write(path: string, value: unknown): void {
  const temporary = path + '.' + randomUUID() + '.tmp'
  writeFileSync(temporary, JSON.stringify(value, null, 2), 'utf8'); renameSync(temporary, path)
}
export function auditProcessAlive(pid: number): boolean { try { process.kill(pid, 0); return true } catch (error) { return (error as NodeJS.ErrnoException).code === 'EPERM' } }

/** Pure filesystem observation: no subprocess, network call, retry or state write. */
export function readProjectAuditStatus(directory: string, context: AuditContext, owner?: { pid: number; nonce: string; active: boolean }, alive = auditProcessAlive): ProjectAuditStatus {
  const state = auditJson(statePath(directory)) as AuditLifecycle | undefined
  const pending = auditJson(join(directory, 'current-audit-pending.json'))
  const previousPresent = existsSync(join(directory, 'current-audit-previous.json'))
  if (!state) return { phase: pending ? 'interrupted' : 'missing', error: pending?.error || (pending ? 'AUDIT_INTERRUPTED: previous audit has no live owner' : undefined), applicability: existsSync(join(directory, 'current-audit.json')) ? 'unbound-evidence' : undefined, previousPresent }
  const owned = owner?.active === true && state.ownerPid === owner.pid && state.ownerNonce === owner.nonce
  const external = state.ownerPid !== owner?.pid && Number.isSafeInteger(state.ownerPid) && state.ownerPid > 0 && alive(state.ownerPid)
  const interrupted = state.phase === 'running' && !owned && !external
  return { phase: interrupted ? 'interrupted' : state.phase, operationId: state.operationId, startedAt: state.startedAt, finishedAt: state.finishedAt, error: interrupted ? 'AUDIT_INTERRUPTED: audit process did not survive restart' : state.error, applicability: auditContextDifference(state.evidenceContext, context), previousPresent }
}

/** Explicit operation owner. Duplicate clicks join the same attempt; status and
 * restart observation never spend an attempt or launch another collector. */
export function createProjectAuditController(dependencies: { run: (request: AuditRequest, operationId: string) => Promise<void>; now?: () => number; alive?: (pid: number) => boolean; event?: (request: AuditRequest, text: string) => void }) {
  const jobs = new Map<string, AuditJob>()
  const resources = new Map<string, string>()
  const nonce = randomUUID()
  const now = dependencies.now ?? Date.now
  const emit = (request: AuditRequest, text: string) => { try { dependencies.event?.(request, text) } catch { /* diagnostics never own audit authority */ } }
  const status = (request: Pick<AuditRequest, 'runDir' | 'context'>) => readProjectAuditStatus(request.runDir, request.context, { pid: process.pid, nonce, active: Boolean(jobs.get(request.runDir) && !jobs.get(request.runDir)?.finishedAt) }, dependencies.alive)
  return {
    jobs,
    status,
    active: () => [...jobs.values()].some(job => !job.finishedAt),
    start(request: AuditRequest): ProjectAuditStatus {
      const previous = jobs.get(request.runDir)
      if (previous && !previous.finishedAt) return status(request)
      const observed = status(request)
      if (observed.phase === 'running') throw new Error('AUDIT_SCOPE_BUSY: another process owns this audit')
      const resource = request.context.resource
      if (resources.has(resource)) throw new Error('AUDIT_SCOPE_BUSY: another project is auditing the shared manager workspace')
      mkdirSync(request.runDir, { recursive: true })
      const oldState = auditJson(statePath(request.runDir)) as AuditLifecycle | undefined
      const previousEvidence = auditJson(join(request.runDir, 'current-audit.json'))
      if (previousEvidence && ['PASS', 'FAIL'].includes(previousEvidence.status) && previousEvidence.auditComplete === true) write(join(request.runDir, 'current-audit-previous.json'), previousEvidence)
      const operationId = randomUUID()
      let state: AuditLifecycle = { schemaVersion: 1, operationId, phase: 'running', context: request.context, evidenceContext: oldState?.evidenceContext, ownerPid: process.pid, ownerNonce: nonce, startedAt: now() }
      const pending = join(request.runDir, 'current-audit-pending.json')
      write(statePath(request.runDir), state)
      write(pending, { operationId, identity: request.context.hash, ownerPid: process.pid })
      const job: AuditJob = { identity: request.context.hash, resource, promise: Promise.resolve() }
      resources.set(resource, request.runDir)
      jobs.set(request.runDir, job)
      emit(request, 'Audit started: ' + operationId)
      job.promise = (async () => {
        try {
          await dependencies.run(request, operationId)
          if (request.currentContext().hash !== request.context.hash) throw new Error('AUDIT_INPUT_CHANGED: audit context changed during collection')
          const collected = auditJson(join(request.runDir, 'current-audit.json'))
          if (collected?.operationId !== operationId || collected?.desktopContextHash !== request.context.hash) throw new Error('AUDIT_EVIDENCE_MISSING: collector did not publish evidence for this operation')
          state = { ...state, phase: 'done', evidenceContext: request.context, finishedAt: now() }
          write(statePath(request.runDir), state)
          if (existsSync(pending)) unlinkSync(pending)
          emit(request, 'Audit completed: ' + operationId)
        } catch (error) {
          job.error = error instanceof Error ? error.message : String(error)
          state = { ...state, phase: 'error', error: job.error, finishedAt: now() }
          write(statePath(request.runDir), state)
          write(pending, { operationId, identity: request.context.hash, ownerPid: process.pid, error: job.error })
          emit(request, 'Audit failed: ' + job.error)
        } finally { job.finishedAt = now(); resources.delete(resource) }
      })()
      return status(request)
    },
  }
}
