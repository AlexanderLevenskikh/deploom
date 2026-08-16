import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, isAbsolute, relative, resolve } from 'node:path'
import type { ProvenDependencyEnvelope } from './migration-progress.js'

export type ProvenResolvedState = {
  key: string
  resolverInputKey: string
  manager: string
  lockfilePath: string
  lockfileHash: string
  observedResolvedHash: string
  artifactRelativePath: string
  artifactBytes: Buffer
}

function sha256(value: string | Buffer): string {
  return createHash('sha256').update(value).digest('hex')
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
  if (value && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b))
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`).join(',')}}`
  }
  return JSON.stringify(value)
}

export function resolvedStateContentKey(input: {
  resolverInputKey: string
  manager: string
  lockfilePath: string
  lockfileHash: string
  observedResolvedHash: string
}): string {
  return sha256(canonicalJson({
    schema: 'resolved-state-v1',
    resolverInputKey: input.resolverInputKey,
    manager: input.manager,
    lockfilePath: input.lockfilePath,
    lockfileHash: input.lockfileHash,
    observedResolvedHash: input.observedResolvedHash,
  }))
}

function proofCacheRoot(workspacePath: string): string {
  return resolve(workspacePath, '.dependency-roadmap', 'cache', 'baseline-proofs')
}

function inside(candidate: string, root: string): boolean {
  const rel = relative(root, candidate)
  return rel === '' || (!rel.startsWith('..') && !isAbsolute(rel))
}

export function loadProvenResolvedState(
  workspacePath: string,
  envelope: ProvenDependencyEnvelope,
): ProvenResolvedState {
  const root = proofCacheRoot(workspacePath)
  const resolverProofPath = resolve(root, 'resolver', `${envelope.resolverInputKey}.json`)
  if (!inside(resolverProofPath, root) || !existsSync(resolverProofPath)) {
    throw new Error(`PROVEN_RESOLVED_STATE_PROOF_MISSING: ${envelope.resolverInputKey}`)
  }

  let proof: Record<string, unknown>
  try {
    proof = JSON.parse(readFileSync(resolverProofPath, 'utf8')) as Record<string, unknown>
  } catch (error) {
    throw new Error(`PROVEN_RESOLVED_STATE_PROOF_INVALID: ${error instanceof Error ? error.message : String(error)}`)
  }
  if (
    proof.schemaVersion !== 1
    || proof.proofSchema !== envelope.proofSchema
    || proof.proofType !== 'resolver'
    || proof.key !== envelope.resolverInputKey
    || proof.outcome !== 'passed'
  ) {
    throw new Error('PROVEN_RESOLVED_STATE_PROOF_IDENTITY_MISMATCH')
  }

  const metadata = proof.metadata
  if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) {
    throw new Error('PROVEN_RESOLVED_STATE_METADATA_MISSING')
  }
  const raw = metadata as Record<string, unknown>
  const key = String(raw.resolvedStateKey ?? '')
  const resolverInputKey = String(raw.resolvedStateResolverInputKey ?? '')
  const manager = String(raw.resolvedPackageManager ?? '')
  const lockfilePath = String(raw.resolvedLockfilePath ?? '')
  const lockfileHash = String(raw.resolvedLockfileHash ?? '')
  const artifactRelativePath = String(raw.resolvedStateArtifact ?? '')
  const observedResolvedHash = String(raw.resolvedStateObservedHash ?? '')

  if (
    !/^[0-9a-f]{64}$/i.test(key)
    || key !== envelope.resolvedStateKey
    || resolverInputKey !== envelope.resolverInputKey
    || !manager
    || !lockfilePath
    || lockfilePath !== envelope.resolvedLockfilePath
    || !/^[0-9a-f]{64}$/i.test(lockfileHash)
    || lockfileHash !== envelope.resolvedLockfileHash
    || !artifactRelativePath
    || observedResolvedHash !== envelope.observedResolvedHash
  ) {
    throw new Error('PROVEN_RESOLVED_STATE_METADATA_INVALID')
  }

  const artifact = resolve(root, artifactRelativePath)
  if (!inside(artifact, root) || !existsSync(artifact)) {
    throw new Error('PROVEN_RESOLVED_STATE_ARTIFACT_MISSING')
  }
  const artifactBytes = readFileSync(artifact)
  if (sha256(artifactBytes) !== lockfileHash) {
    throw new Error('PROVEN_RESOLVED_STATE_ARTIFACT_HASH_MISMATCH')
  }
  const expectedKey = resolvedStateContentKey({
    resolverInputKey,
    manager,
    lockfilePath,
    lockfileHash,
    observedResolvedHash,
  })
  if (expectedKey !== key) throw new Error('PROVEN_RESOLVED_STATE_KEY_MISMATCH')

  return {
    key,
    resolverInputKey,
    manager,
    lockfilePath,
    lockfileHash,
    observedResolvedHash,
    artifactRelativePath,
    artifactBytes,
  }
}

export function resolvedStateTargetPath(gitRoot: string, state: ProvenResolvedState): string {
  const root = resolve(gitRoot)
  const target = resolve(root, state.lockfilePath)
  if (!inside(target, root)) throw new Error(`PROVEN_RESOLVED_STATE_PATH_ESCAPE: ${state.lockfilePath}`)
  return target
}

export function restoreProvenResolvedStateLockfile(
  gitRoot: string,
  state: ProvenResolvedState,
): string {
  const target = resolvedStateTargetPath(gitRoot, state)
  mkdirSync(dirname(target), { recursive: true })
  writeFileSync(target, state.artifactBytes)
  verifyProvenResolvedStateLockfile(gitRoot, state)
  return target
}

export function verifyProvenResolvedStateLockfile(
  gitRoot: string,
  state: ProvenResolvedState,
): void {
  const target = resolvedStateTargetPath(gitRoot, state)
  if (!existsSync(target)) throw new Error(`PROVEN_RESOLVED_STATE_LOCKFILE_MISSING: ${state.lockfilePath}`)
  const current = sha256(readFileSync(target))
  if (current !== state.lockfileHash) {
    throw new Error(`PROVEN_RESOLVED_STATE_LOCKFILE_DRIFT: expected=${state.lockfileHash} current=${current}`)
  }
}
