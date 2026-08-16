import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'

export type MaterializationAction = {
  package: string
  target: string
  action: string
  section?: string
}

export type DependencyMaterializationProof = {
  schemaVersion: 2
  project: string
  branch: string
  assignmentHash: string
  actions: MaterializationAction[]
  provenEnvelopeKey: string
  provenAssignmentKey: string
  resolverInputKey: string
  sourceSnapshotKey: string
  projectProofKey: string
  dependencySections: Record<string, Record<string, string>>
  dependencySectionsHash: string
  dependencyControlFields: Record<string, unknown>
  dependencyStateHash: string
  lockfiles: Record<string, string>
  observedResolvedVersions: Record<string, string>
  observedResolvedHash: string
  packageManager: string
  packageManagerVersion: string
  nodeVersion: string
  gitHead: string
  createdAt: string
}

const DEPENDENCY_SECTIONS = ['dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies'] as const
const LOCKFILES = ['yarn.lock', 'pnpm-lock.yaml', 'package-lock.json'] as const
export const DEPENDENCY_CONTROL_KEYS = ['resolutions', 'overrides', 'pnpm', 'packageManager', 'engines', 'os', 'cpu', 'workspaces'] as const

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

export function normalizeMaterializationActions(actions: readonly MaterializationAction[]): MaterializationAction[] {
  return actions
    .map((item) => ({
      package: String(item.package),
      target: String(item.target),
      action: String(item.action || 'update'),
      ...(item.section ? { section: String(item.section) } : {}),
    }))
    .sort((a, b) =>
      a.package.localeCompare(b.package)
      || a.target.localeCompare(b.target)
      || a.action.localeCompare(b.action)
      || String(a.section ?? '').localeCompare(String(b.section ?? '')))
}

export function materializationAssignmentHash(actions: readonly MaterializationAction[]): string {
  return sha256(canonicalJson(normalizeMaterializationActions(actions))).slice(0, 24)
}

export function dependencySectionsFromPackageJson(packageJson: unknown): Record<string, Record<string, string>> {
  const root = packageJson && typeof packageJson === 'object' && !Array.isArray(packageJson) ? packageJson as Record<string, unknown> : {}
  const result: Record<string, Record<string, string>> = {}
  for (const section of DEPENDENCY_SECTIONS) {
    const raw = root[section]
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue
    result[section] = Object.fromEntries(
      Object.entries(raw as Record<string, unknown>)
        .filter(([, value]) => typeof value === 'string')
        .map(([name, value]) => [name, String(value)])
        .sort(([a], [b]) => a.localeCompare(b)),
    )
  }
  return result
}

export function dependencySectionsHash(packageJson: unknown): string {
  return sha256(canonicalJson(dependencySectionsFromPackageJson(packageJson)))
}

export function dependencyControlFieldsFromPackageJson(packageJson: unknown): Record<string, unknown> {
  const root = packageJson && typeof packageJson === 'object' && !Array.isArray(packageJson) ? packageJson as Record<string, unknown> : {}
  const result: Record<string, unknown> = {}
  for (const key of DEPENDENCY_CONTROL_KEYS) {
    if (root[key] !== undefined) result[key] = root[key]
  }
  return result
}

export function dependencyStateHash(packageJson: unknown): string {
  return sha256(canonicalJson({
    dependencySections: dependencySectionsFromPackageJson(packageJson),
    dependencyControlFields: dependencyControlFieldsFromPackageJson(packageJson),
  }))
}

export function lockfileHashes(projectPath: string): Record<string, string> {
  const result: Record<string, string> = {}
  for (const name of LOCKFILES) {
    const path = join(projectPath, name)
    if (existsSync(path)) result[name] = sha256(readFileSync(path))
  }
  return result
}

function installedPackageJsonPath(projectPath: string, packageName: string): string | undefined {
  const parts = packageName.startsWith('@') ? packageName.split('/') : [packageName]
  let cursor = resolve(projectPath)
  while (true) {
    const candidate = join(cursor, 'node_modules', ...parts, 'package.json')
    if (existsSync(candidate)) return candidate
    const parent = dirname(cursor)
    if (parent === cursor) return undefined
    cursor = parent
  }
}

export function observedResolvedVersions(
  projectPath: string,
  actions: readonly MaterializationAction[],
): Record<string, string> {
  const observed: Record<string, string> = {}
  for (const action of normalizeMaterializationActions(actions)) {
    if (action.action !== 'update' || action.section === 'peerDependencies') continue

    const packagePath = installedPackageJsonPath(projectPath, action.package)
    if (!packagePath) {
      if (action.section === 'optionalDependencies') {
        observed[action.package] = '<optional-not-installed>'
        continue
      }
      throw new Error(`OBSERVED_RESOLVED_ASSIGNMENT_MISSING: ${action.package}@${action.target}`)
    }

    let version = ''
    try {
      const raw = JSON.parse(readFileSync(packagePath, 'utf8')) as { version?: unknown }
      version = typeof raw.version === 'string' ? raw.version.trim() : ''
    } catch (error) {
      throw new Error(`OBSERVED_RESOLVED_ASSIGNMENT_INVALID: ${action.package}: ${error instanceof Error ? error.message : String(error)}`)
    }
    if (version !== action.target) {
      throw new Error(`OBSERVED_RESOLVED_ASSIGNMENT_DRIFT: ${action.package} expected=${action.target} observed=${version || '<missing-version>'}`)
    }
    observed[action.package] = version
  }
  return Object.fromEntries(Object.entries(observed).sort(([a], [b]) => a.localeCompare(b)))
}

export function materializationProofPath(workspacePath: string, project: string, branch: string): string {
  const safe = (value: string) => value.replace(/[^a-zA-Z0-9._-]+/g, '-')
  return join(workspacePath, '.dependency-roadmap', 'state', 'materialization-proofs', safe(project), `${safe(branch)}.json`)
}

export function createMaterializationProof(input: {
  projectPath: string
  project: string
  branch: string
  actions: readonly MaterializationAction[]
  provenEnvelopeKey: string
  provenAssignmentKey: string
  resolverInputKey: string
  sourceSnapshotKey: string
  projectProofKey: string
  packageManager: string
  packageManagerVersion: string
  nodeVersion: string
  gitHead: string
  createdAt?: string
}): DependencyMaterializationProof {
  const packageJson = JSON.parse(readFileSync(join(input.projectPath, 'package.json'), 'utf8')) as unknown
  const actions = normalizeMaterializationActions(input.actions)
  const dependencySections = dependencySectionsFromPackageJson(packageJson)
  const dependencyControlFields = dependencyControlFieldsFromPackageJson(packageJson)
  const observed = observedResolvedVersions(input.projectPath, actions)

  return {
    schemaVersion: 2,
    project: input.project,
    branch: input.branch,
    assignmentHash: materializationAssignmentHash(actions),
    actions,
    provenEnvelopeKey: input.provenEnvelopeKey,
    provenAssignmentKey: input.provenAssignmentKey,
    resolverInputKey: input.resolverInputKey,
    sourceSnapshotKey: input.sourceSnapshotKey,
    projectProofKey: input.projectProofKey,
    dependencySections,
    dependencySectionsHash: sha256(canonicalJson(dependencySections)),
    dependencyControlFields,
    dependencyStateHash: sha256(canonicalJson({ dependencySections, dependencyControlFields })),
    lockfiles: lockfileHashes(input.projectPath),
    observedResolvedVersions: observed,
    observedResolvedHash: sha256(canonicalJson(observed)),
    packageManager: input.packageManager,
    packageManagerVersion: input.packageManagerVersion,
    nodeVersion: input.nodeVersion,
    gitHead: input.gitHead,
    createdAt: input.createdAt ?? new Date().toISOString(),
  }
}

export function writeMaterializationProof(path: string, proof: DependencyMaterializationProof): void {
  mkdirSync(dirname(path), { recursive: true })
  const temporary = `${path}.tmp`
  writeFileSync(temporary, `${JSON.stringify(proof, null, 2)}\n`, 'utf8')
  renameSync(temporary, path)
}

export function readMaterializationProof(path: string): DependencyMaterializationProof | undefined {
  if (!existsSync(path)) return undefined
  try {
    const value = JSON.parse(readFileSync(path, 'utf8')) as DependencyMaterializationProof
    return value?.schemaVersion === 2 ? value : undefined
  } catch {
    return undefined
  }
}

export function validateMaterializationProof(input: {
  projectPath: string
  proof: DependencyMaterializationProof | undefined
  project: string
  branch: string
  actions: readonly MaterializationAction[]
  packageManager: string
  packageManagerVersion?: string
  nodeVersion?: string
  provenEnvelopeKey?: string
  provenAssignmentKey?: string
  resolverInputKey?: string
  sourceSnapshotKey?: string
  projectProofKey?: string
}): { ok: boolean; reason: string } {
  const proof = input.proof
  if (!proof) return { ok: false, reason: 'proof missing' }
  if (proof.project !== input.project || proof.branch !== input.branch) return { ok: false, reason: 'project/branch identity mismatch' }
  if (proof.assignmentHash !== materializationAssignmentHash(input.actions)) return { ok: false, reason: 'assignment hash mismatch' }
  if (input.provenEnvelopeKey && proof.provenEnvelopeKey !== input.provenEnvelopeKey) return { ok: false, reason: 'proven envelope mismatch' }
  if (input.provenAssignmentKey && proof.provenAssignmentKey !== input.provenAssignmentKey) return { ok: false, reason: 'proven assignment mismatch' }
  if (input.resolverInputKey && proof.resolverInputKey !== input.resolverInputKey) return { ok: false, reason: 'resolver proof identity mismatch' }
  if (input.sourceSnapshotKey && proof.sourceSnapshotKey !== input.sourceSnapshotKey) return { ok: false, reason: 'source snapshot identity mismatch' }
  if (input.projectProofKey && proof.projectProofKey !== input.projectProofKey) return { ok: false, reason: 'project proof identity mismatch' }
  if (proof.packageManager !== input.packageManager) return { ok: false, reason: 'package-manager mismatch' }
  if (input.packageManagerVersion && proof.packageManagerVersion !== input.packageManagerVersion) return { ok: false, reason: 'package-manager version mismatch' }
  if (input.nodeVersion && proof.nodeVersion !== input.nodeVersion) return { ok: false, reason: 'Node version mismatch' }

  let packageJson: unknown
  try {
    packageJson = JSON.parse(readFileSync(join(input.projectPath, 'package.json'), 'utf8')) as unknown
  } catch {
    return { ok: false, reason: 'package.json unreadable' }
  }
  if (dependencySectionsHash(packageJson) !== proof.dependencySectionsHash) return { ok: false, reason: 'dependency sections changed' }
  if (dependencyStateHash(packageJson) !== proof.dependencyStateHash) return { ok: false, reason: 'dependency control fields changed' }

  const currentLockfiles = lockfileHashes(input.projectPath)
  if (canonicalJson(currentLockfiles) !== canonicalJson(proof.lockfiles)) return { ok: false, reason: 'lockfile digest changed' }

  let observed: Record<string, string>
  try {
    observed = observedResolvedVersions(input.projectPath, proof.actions)
  } catch (error) {
    return { ok: false, reason: error instanceof Error ? error.message : String(error) }
  }
  if (sha256(canonicalJson(observed)) !== proof.observedResolvedHash) {
    return { ok: false, reason: 'observed resolved tuple changed' }
  }
  return { ok: true, reason: 'materialization proof matches ProofEnvelope and current dependency state' }
}
