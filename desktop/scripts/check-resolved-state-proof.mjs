import { createHash } from 'node:crypto'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  loadProvenResolvedState,
  resolvedStateContentKey,
  restoreProvenResolvedStateLockfile,
  verifyProvenResolvedStateLockfile,
} from '../dist-electron/resolved-state-proof.js'

const workspace = mkdtempSync(join(tmpdir(), 'deploom-resolved-state-proof-'))
try {
  const proofRoot = join(workspace, '.dependency-roadmap', 'cache', 'baseline-proofs')
  const resolverInputKey = 'b'.repeat(32)
  const observedResolvedHash = 'c'.repeat(64)
  const manager = 'yarn'
  const lockfilePath = 'yarn.lock'
  const lockfileBytes = Buffer.from('a@1.0.0:\n  version "1.0.0"\n')
  const lockfileHash = createHash('sha256').update(lockfileBytes).digest('hex')
  const key = resolvedStateContentKey({
    resolverInputKey,
    manager,
    lockfilePath,
    lockfileHash,
    observedResolvedHash,
  })
  const artifactRelativePath = `resolved-state/${key}/lockfile.bin`
  const artifact = join(proofRoot, artifactRelativePath)
  mkdirSync(join(proofRoot, 'resolver'), { recursive: true })
  mkdirSync(join(proofRoot, 'resolved-state', key), { recursive: true })
  writeFileSync(artifact, lockfileBytes)

  const envelope = {
    schemaVersion: 3,
    proofSchema: 'baseline-proof-v5-resolved-state',
    envelopeKey: 'unused-in-this-test',
    project: 'Demo',
    mode: 'yellow',
    sourceHead: 'deadbeef',
    sourceSnapshotKey: 'source',
    assignmentKey: 'assignment',
    resolverInputKey,
    preparationProofKey: 'prep',
    projectProofKey: 'project',
    observedResolvedHash,
    resolvedStateKey: key,
    resolvedLockfilePath: lockfilePath,
    resolvedLockfileHash: lockfileHash,
    exactDirectAssignment: { a: '1.0.0' },
    removals: [],
    verificationCommands: [],
    projectChecks: 'adaptive',
    resolverProofStatus: 'passed',
    preparationProofStatus: 'passed',
    projectProofStatus: 'passed',
  }
  const proof = {
    schemaVersion: 1,
    proofSchema: envelope.proofSchema,
    proofType: 'resolver',
    key: resolverInputKey,
    outcome: 'passed',
    identity: {},
    metadata: {
      observedResolvedVersions: { a: '1.0.0' },
      observedResolvedHash,
      resolvedStateKey: key,
      resolvedStateResolverInputKey: resolverInputKey,
      resolvedPackageManager: manager,
      resolvedLockfilePath: lockfilePath,
      resolvedLockfileHash: lockfileHash,
      resolvedStateArtifact: artifactRelativePath,
      resolvedStateObservedHash: observedResolvedHash,
    },
  }
  writeFileSync(
    join(proofRoot, 'resolver', `${resolverInputKey}.json`),
    `${JSON.stringify(proof, null, 2)}\n`,
  )

  const state = loadProvenResolvedState(workspace, envelope)
  if (state.key !== key || state.lockfileHash !== lockfileHash) {
    throw new Error('resolved state was not loaded exactly')
  }

  const project = join(workspace, 'project')
  mkdirSync(project, { recursive: true })
  restoreProvenResolvedStateLockfile(project, state)
  verifyProvenResolvedStateLockfile(project, state)
  if (!readFileSync(join(project, 'yarn.lock')).equals(lockfileBytes)) {
    throw new Error('exact lockfile bytes were not restored')
  }

  writeFileSync(artifact, 'tampered\n')
  let rejected = false
  try {
    loadProvenResolvedState(workspace, envelope)
  } catch (error) {
    rejected = String(error).includes('HASH')
  }
  if (!rejected) throw new Error('tampered ResolvedState artifact must fail closed')

  console.log('ResolvedState proof artifact contract OK')
} finally {
  rmSync(workspace, { recursive: true, force: true })
}
