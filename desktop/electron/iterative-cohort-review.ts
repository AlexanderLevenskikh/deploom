import { readFileSync, writeFileSync, renameSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'

function read(path: string): Record<string, any> | undefined {
  try { return JSON.parse(readFileSync(path, 'utf8').replace(/^\uFEFF/, '')) } catch { return undefined }
}
const assignment = (value: Record<string, unknown> = {}) => JSON.stringify(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)))
export function cohortReviewEnabled(runDir: string): boolean {
  return read(join(runDir, 'cohort-review-settings.json'))?.enabled !== false
}
export function saveCohortReviewEnabled(runDir: string, enabled: boolean): void {
  mkdirSync(runDir, { recursive: true })
  const path = join(runDir, 'cohort-review-settings.json')
  writeFileSync(`${path}.tmp`, JSON.stringify({ enabled }), 'utf8'); renameSync(`${path}.tmp`, path)
}
export function pendingCohortReview(runDir: string, payload?: Record<string, any>) {
  const run = payload?.run ?? read(join(runDir, 'run.json'))
  const candidate = payload?.candidate ?? read(join(runDir, 'trial', 'candidate.json'))
  if (!cohortReviewEnabled(runDir) || !candidate || candidate.stage !== 'PLANNED' || run?.terminal || candidate.candidateId !== run?.activeCandidateId || candidate.baseCheckpointId !== run?.activeCheckpointId) return
  const checkpoint = payload?.activeCheckpoint ?? read(join(runDir, 'checkpoints', `${candidate.baseCheckpointId}.json`))
  const approval = read(join(runDir, 'cohort-approval.json'))
  if (approval && approval.candidateId === candidate.candidateId && approval.baseCheckpointId === candidate.baseCheckpointId && approval.policyHash === candidate.policyHash && (approval.sourceSnapshotKey ?? null) === (checkpoint?.sourceSnapshotKey ?? null) && assignment(approval.fullAssignment) === assignment(candidate.fullAssignment)) return
  return { candidateId: String(candidate.candidateId), checkpointId: String(candidate.baseCheckpointId),
    packages: Object.entries(candidate.fullAssignment ?? {}).filter(([name, target]) => target !== checkpoint?.fullAssignment?.[name]).map(([name, target]) => ({ name, current: String(checkpoint?.fullAssignment?.[name] ?? ''), target: String(target) })),
    atomicGroups: candidate.atomicGroups as string[][] | undefined }
}
