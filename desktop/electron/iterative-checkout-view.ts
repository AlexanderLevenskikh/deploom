import { existsSync } from 'node:fs'
import { isAbsolute, join, relative, resolve } from 'node:path'

export type IterativeCheckoutView = { path: string; kind: 'trial' | 'checkpoint' | 'project'; checkpointId?: string }

// Display durable checkout coordinates. Opening a folder has no Git authority.
export function iterativeCheckoutView(payload: Record<string, any>, runDir: string, projectPath: string): IterativeCheckoutView | undefined {
  const contained = (root: string, target: string) => {
    const rel = relative(resolve(root), resolve(target))
    return rel !== '..' && !rel.startsWith('..\\') && !rel.startsWith('../') && !isAbsolute(rel)
  }
  const trial = (refs: Record<string, unknown> | undefined): IterativeCheckoutView | undefined => {
    if (typeof refs?.workspaceRoot !== 'string' || !isAbsolute(refs.workspaceRoot)) return
    const root = resolve(refs.workspaceRoot)
    const path = resolve(root, typeof refs.projectRelative === 'string' ? refs.projectRelative : '.')
    if (!contained(join(runDir, 'trial'), root) || !contained(root, path) || !existsSync(path)) return
    return { path, kind: 'trial', checkpointId: payload.activeCheckpoint?.checkpointId }
  }
  const active = trial(payload.candidate?.materializationRefs)
  if (active) return active
  if (payload.run?.phase === 'BLOCKED_C0' || payload.run?.bootstrapRefs) {
    const bootstrap = trial(payload.run?.bootstrapRefs)
    if (bootstrap) return bootstrap
  }
  const snapshot = payload.activeCheckpoint?.sourceSnapshotContainer
  if (typeof snapshot === 'string' && isAbsolute(snapshot) && contained(runDir, snapshot) && existsSync(snapshot)) {
    return { path: resolve(snapshot), kind: 'checkpoint', checkpointId: payload.activeCheckpoint?.checkpointId }
  }
  if (existsSync(projectPath)) return { path: resolve(projectPath), kind: 'project' }
  return undefined
}
