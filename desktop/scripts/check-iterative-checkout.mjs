import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { iterativeCheckoutView } from '../dist-electron/iterative-checkout-view.js'
const root = mkdtempSync(join(tmpdir(), 'deploom-checkout-'))
try {
  const run = join(root, 'run'), project = join(root, 'project'), trial = join(run, 'trial', 'workspace')
  const snapshot = join(run, 'source', 'C6')
  for (const path of [project, trial, snapshot]) mkdirSync(path, { recursive: true })
  const base = { activeCheckpoint: { checkpointId: 'C6', sourceSnapshotContainer: snapshot } }
  assert.equal(iterativeCheckoutView(base, run, project).kind, 'checkpoint')
  const active = { ...base, candidate: { materializationRefs: { workspaceRoot: trial, projectRelative: '.' } } }
  assert.deepEqual(iterativeCheckoutView(active, run, project), { path: trial, kind: 'trial', checkpointId: 'C6' })
  assert.equal(iterativeCheckoutView({ ...base, run: { phase: 'BLOCKED_C0', bootstrapRefs: active.candidate.materializationRefs } }, run, project).kind, 'trial')
  assert.equal(iterativeCheckoutView({ ...active, candidate: { materializationRefs: { workspaceRoot: project } } }, run, project).kind, 'checkpoint')
  assert.equal(iterativeCheckoutView({ ...active, candidate: { materializationRefs: { workspaceRoot: trial, projectRelative: '../../../project' } } }, run, project).kind, 'checkpoint')
  assert.equal(iterativeCheckoutView({ ...active, candidate: { materializationRefs: { workspaceRoot: 'relative' } } }, run, project).kind, 'checkpoint')
  assert.equal(iterativeCheckoutView({}, run, project).kind, 'project')
  console.log('check-iterative-checkout: OK (trial, bootstrap, checkpoint, original and path boundaries)')
} finally { rmSync(root, { recursive: true, force: true }) }
