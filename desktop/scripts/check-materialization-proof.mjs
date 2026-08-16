import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  createMaterializationProof,
  materializationAssignmentHash,
  readMaterializationProof,
  validateMaterializationProof,
  writeMaterializationProof,
} from '../dist-electron/materialization-proof.js'

const dir = mkdtempSync(join(tmpdir(), 'dependency-flow-materialization-proof-'))
try {
  const packagePath = join(dir, 'package.json')
  const lockPath = join(dir, 'yarn.lock')
  const installedPath = join(dir, 'node_modules', 'a')
  mkdirSync(installedPath, { recursive: true })
  writeFileSync(join(installedPath, 'package.json'), JSON.stringify({ name: 'a', version: '2.0.0' }))
  writeFileSync(packagePath, JSON.stringify({
    scripts: { build: 'vite build' },
    dependencies: { a: '2.0.0' },
    devDependencies: { b: '3.0.0' },
    resolutions: { transitive: '1.0.0' },
  }, null, 2) + '\n')
  writeFileSync(lockPath, 'a@2.0.0:\n  version "2.0.0"\n')

  const actions = [{ package: 'a', target: '2.0.0', action: 'update', section: 'dependencies' }]
  const identity = {
    provenEnvelopeKey: 'envelope-key',
    provenAssignmentKey: 'assignment-key',
    resolverInputKey: 'resolver-key',
    sourceSnapshotKey: 'source-key',
    projectProofKey: 'project-key',
  }
  const proof = createMaterializationProof({
    projectPath: dir,
    project: 'Demo',
    branch: 'CD-1-demo',
    actions,
    ...identity,
    packageManager: 'yarn',
    packageManagerVersion: '1.22.22',
    nodeVersion: 'v24.0.0',
    gitHead: 'deadbeef',
    createdAt: '2026-08-14T00:00:00.000Z',
  })
  if (proof.schemaVersion !== 2) throw new Error('proof schema was not upgraded')
  if (proof.assignmentHash !== materializationAssignmentHash(actions)) throw new Error('assignment hash is not deterministic')
  if (proof.observedResolvedVersions.a !== '2.0.0') throw new Error(`resolved tuple was not observed: ${JSON.stringify(proof.observedResolvedVersions)}`)

  const proofPath = join(dir, 'proof.json')
  writeMaterializationProof(proofPath, proof)
  const read = readMaterializationProof(proofPath)
  let result = validateMaterializationProof({
    projectPath: dir, proof: read, project: 'Demo', branch: 'CD-1-demo',
    actions, ...identity, packageManager: 'yarn', packageManagerVersion: '1.22.22', nodeVersion: 'v24.0.0',
  })
  if (!result.ok) throw new Error(`fresh proof must validate: ${result.reason}`)

  result = validateMaterializationProof({
    projectPath: dir, proof: read, project: 'Demo', branch: 'CD-1-demo',
    actions, ...identity, provenEnvelopeKey: 'different-envelope',
    packageManager: 'yarn', packageManagerVersion: '1.22.22', nodeVersion: 'v24.0.0',
  })
  if (result.ok || !result.reason.includes('envelope')) throw new Error('envelope drift must invalidate proof')

  // Unrelated scripts may change during semantic migration.
  writeFileSync(packagePath, JSON.stringify({
    scripts: { build: 'vite build --emptyOutDir' },
    dependencies: { a: '2.0.0' },
    devDependencies: { b: '3.0.0' },
    resolutions: { transitive: '1.0.0' },
  }, null, 2) + '\n')
  result = validateMaterializationProof({
    projectPath: dir, proof: read, project: 'Demo', branch: 'CD-1-demo',
    actions, ...identity, packageManager: 'yarn', packageManagerVersion: '1.22.22', nodeVersion: 'v24.0.0',
  })
  if (!result.ok) throw new Error(`non-dependency package.json edits must not invalidate proof: ${result.reason}`)

  writeFileSync(packagePath, JSON.stringify({
    dependencies: { a: '2.1.0' },
    devDependencies: { b: '3.0.0' },
    resolutions: { transitive: '1.0.0' },
  }, null, 2) + '\n')
  result = validateMaterializationProof({
    projectPath: dir, proof: read, project: 'Demo', branch: 'CD-1-demo',
    actions, ...identity, packageManager: 'yarn', packageManagerVersion: '1.22.22', nodeVersion: 'v24.0.0',
  })
  if (result.ok || !result.reason.includes('dependency sections')) throw new Error('dependency mutation must invalidate proof')

  writeFileSync(packagePath, JSON.stringify({
    dependencies: { a: '2.0.0' },
    devDependencies: { b: '3.0.0' },
    resolutions: { transitive: '1.0.0' },
  }, null, 2) + '\n')
  writeFileSync(join(installedPath, 'package.json'), JSON.stringify({ name: 'a', version: '2.1.0' }))
  result = validateMaterializationProof({
    projectPath: dir, proof: read, project: 'Demo', branch: 'CD-1-demo',
    actions, ...identity, packageManager: 'yarn', packageManagerVersion: '1.22.22', nodeVersion: 'v24.0.0',
  })
  if (result.ok || !result.reason.includes('OBSERVED_RESOLVED_ASSIGNMENT_DRIFT')) throw new Error(`resolved tuple mutation must invalidate proof: ${result.reason}`)

  writeFileSync(join(installedPath, 'package.json'), JSON.stringify({ name: 'a', version: '2.0.0' }))
  writeFileSync(packagePath, JSON.stringify({
    dependencies: { a: '2.0.0' },
    devDependencies: { b: '3.0.0' },
    resolutions: { transitive: '2.0.0' },
  }, null, 2) + '\n')
  result = validateMaterializationProof({
    projectPath: dir, proof: read, project: 'Demo', branch: 'CD-1-demo',
    actions, ...identity, packageManager: 'yarn', packageManagerVersion: '1.22.22', nodeVersion: 'v24.0.0',
  })
  if (result.ok || !result.reason.includes('dependency control fields')) throw new Error('resolution/override mutation must invalidate proof')

  writeFileSync(packagePath, JSON.stringify({
    dependencies: { a: '2.0.0' },
    devDependencies: { b: '3.0.0' },
    resolutions: { transitive: '1.0.0' },
  }, null, 2) + '\n')
  writeFileSync(lockPath, 'a@2.0.0:\n  version "2.0.1"\n')
  result = validateMaterializationProof({
    projectPath: dir, proof: read, project: 'Demo', branch: 'CD-1-demo',
    actions, ...identity, packageManager: 'yarn', packageManagerVersion: '1.22.22', nodeVersion: 'v24.0.0',
  })
  if (result.ok || !result.reason.includes('lockfile')) throw new Error('lockfile mutation must invalidate proof')

  console.log('Dependency materialization ProofEnvelope contract OK')
} finally {
  rmSync(dir, { recursive: true, force: true })
}
