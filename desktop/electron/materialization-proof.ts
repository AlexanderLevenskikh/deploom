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
  schemaVersion: 4
  project: string
  branch: string
  assignmentHash: string
  actions: MaterializationAction[]
  provenEnvelopeKey: string
  provenAssignmentKey: string
  resolverInputKey: string
  fixedResolverInputsKey: string
  sourceSnapshotKey: string
  projectProofKey: string
  dependencySections: Record<string, Record<string, string>>
  dependencySectionsHash: string
  dependencyControlFields: Record<string, unknown>
  dependencyStateHash: string
  lockfiles: Record<string, string>
  observedResolvedVersions: Record<string, string>
  observedResolvedHash: string
  provenExactDirectAssignment?: Record<string, string>
  provenRemovals?: string[]
  provenObservedResolvedHash?: string
  provenResolvedStateKey: string
  provenResolvedLockfilePath: string
  provenResolvedLockfileHash: string
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


export function observedResolvedDirectAssignment(
  projectPath: string,
  exactAssignment: Readonly<Record<string, string>>,
  removals: readonly string[],
): Record<string, string> {
  const packageJson = JSON.parse(readFileSync(join(projectPath, 'package.json'), 'utf8')) as unknown
  const sections = dependencySectionsFromPackageJson(packageJson)
  const removalSet = new Set(removals.map(String))
  const observed: Record<string, string> = {}

  for (const [packageName, target] of Object.entries(exactAssignment).sort(([a], [b]) => a.localeCompare(b))) {
    if (removalSet.has(packageName)) {
      observed[packageName] = '<removed>'
      continue
    }

    const declaredSections = DEPENDENCY_SECTIONS.filter((section) => sections[section]?.[packageName] !== undefined)
    if (!declaredSections.length) {
      throw new Error(`OBSERVED_RESOLVED_ASSIGNMENT_UNDECLARED: ${packageName}@${target}`)
    }
    if (declaredSections.length === 1 && declaredSections[0] === 'peerDependencies') {
      observed[packageName] = '<peer-only>'
      continue
    }

    const optionalOnly = declaredSections.includes('optionalDependencies')
      && declaredSections.every((section) => section === 'optionalDependencies' || section === 'peerDependencies')
    const packagePath = installedPackageJsonPath(projectPath, packageName)
    if (!packagePath) {
      if (optionalOnly) {
        observed[packageName] = '<optional-not-installed>'
        continue
      }
      throw new Error(`OBSERVED_RESOLVED_ASSIGNMENT_MISSING: ${packageName}@${target}`)
    }

    let version = ''
    try {
      const raw = JSON.parse(readFileSync(packagePath, 'utf8')) as { version?: unknown }
      version = typeof raw.version === 'string' ? raw.version.trim() : ''
    } catch (error) {
      throw new Error(`OBSERVED_RESOLVED_ASSIGNMENT_INVALID: ${packageName}: ${error instanceof Error ? error.message : String(error)}`)
    }
    if (version !== target) {
      throw new Error(`OBSERVED_RESOLVED_ASSIGNMENT_DRIFT: ${packageName} expected=${target} observed=${version || '<missing-version>'}`)
    }
    observed[packageName] = version
  }
  return Object.fromEntries(Object.entries(observed).sort(([a], [b]) => a.localeCompare(b)))
}

export function observedResolvedDirectAssignmentHash(
  projectPath: string,
  exactAssignment: Readonly<Record<string, string>>,
  removals: readonly string[],
): string {
  return sha256(canonicalJson(observedResolvedDirectAssignment(projectPath, exactAssignment, removals)))
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
  fixedResolverInputsKey: string
  sourceSnapshotKey: string
  projectProofKey: string
  provenExactDirectAssignment?: Readonly<Record<string, string>>
  provenRemovals?: readonly string[]
  provenObservedResolvedHash?: string
  provenResolvedStateKey: string
  provenResolvedLockfilePath: string
  provenResolvedLockfileHash: string
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
  const hasProvenObservedContract = Boolean(
    input.provenExactDirectAssignment
    || input.provenRemovals
    || input.provenObservedResolvedHash,
  )
  let provenExactDirectAssignment: Record<string, string> | undefined
  let provenRemovals: string[] | undefined
  let provenObservedResolvedHash: string | undefined
  if (hasProvenObservedContract) {
    const exactAssignment = input.provenExactDirectAssignment
    const removals = input.provenRemovals
    const baselineObservedHash = input.provenObservedResolvedHash
    if (!exactAssignment || !removals || !baselineObservedHash) {
      throw new Error('PROVEN_OBSERVED_RESOLVED_CONTRACT_INCOMPLETE')
    }

    const normalizedAssignment = Object.fromEntries(
      Object.entries(exactAssignment)
        .map(([name, version]) => [String(name), String(version)])
        .sort(([a], [b]) => a.localeCompare(b)),
    )
    const normalizedRemovals = [...new Set(removals.map(String))].sort()
    const currentHash = observedResolvedDirectAssignmentHash(
      input.projectPath,
      normalizedAssignment,
      normalizedRemovals,
    )
    if (currentHash !== baselineObservedHash) {
      throw new Error(
        `OBSERVED_PROVEN_ASSIGNMENT_DRIFT: baseline=${baselineObservedHash} current=${currentHash}`,
      )
    }

    provenExactDirectAssignment = normalizedAssignment
    provenRemovals = normalizedRemovals
    provenObservedResolvedHash = currentHash
  }

  if (!/^[0-9a-f]{64}$/i.test(input.fixedResolverInputsKey)) {
    throw new Error('PROVEN_FIXED_SOURCE_IDENTITY_INVALID')
  }
  if (!/^[0-9a-f]{64}$/i.test(input.provenResolvedStateKey)) {
    throw new Error('PROVEN_RESOLVED_STATE_KEY_INVALID')
  }
  if (!input.provenResolvedLockfilePath || !/^[0-9a-f]{64}$/i.test(input.provenResolvedLockfileHash)) {
    throw new Error('PROVEN_RESOLVED_LOCKFILE_CONTRACT_INVALID')
  }
  const provenLockfile = resolve(input.projectPath, input.provenResolvedLockfilePath)
  if (!existsSync(provenLockfile)) throw new Error(`PROVEN_RESOLVED_LOCKFILE_MISSING: ${input.provenResolvedLockfilePath}`)
  const currentResolvedLockHash = sha256(readFileSync(provenLockfile))
  if (currentResolvedLockHash !== input.provenResolvedLockfileHash) {
    throw new Error(`PROVEN_RESOLVED_LOCKFILE_DRIFT: expected=${input.provenResolvedLockfileHash} current=${currentResolvedLockHash}`)
  }

  return {
    schemaVersion: 4,
    project: input.project,
    branch: input.branch,
    assignmentHash: materializationAssignmentHash(actions),
    actions,
    provenEnvelopeKey: input.provenEnvelopeKey,
    provenAssignmentKey: input.provenAssignmentKey,
    resolverInputKey: input.resolverInputKey,
    fixedResolverInputsKey: input.fixedResolverInputsKey,
    sourceSnapshotKey: input.sourceSnapshotKey,
    projectProofKey: input.projectProofKey,
    dependencySections,
    dependencySectionsHash: sha256(canonicalJson(dependencySections)),
    dependencyControlFields,
    dependencyStateHash: sha256(canonicalJson({ dependencySections, dependencyControlFields })),
    lockfiles: lockfileHashes(input.projectPath),
    observedResolvedVersions: observed,
    observedResolvedHash: sha256(canonicalJson(observed)),
    ...(provenExactDirectAssignment ? { provenExactDirectAssignment } : {}),
    ...(provenRemovals ? { provenRemovals } : {}),
    ...(provenObservedResolvedHash ? { provenObservedResolvedHash } : {}),
    provenResolvedStateKey: input.provenResolvedStateKey,
    provenResolvedLockfilePath: input.provenResolvedLockfilePath,
    provenResolvedLockfileHash: input.provenResolvedLockfileHash,
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
    return value?.schemaVersion === 4 ? value : undefined
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
  fixedResolverInputsKey?: string
  sourceSnapshotKey?: string
  projectProofKey?: string
  provenExactDirectAssignment?: Readonly<Record<string, string>>
  provenRemovals?: readonly string[]
  provenObservedResolvedHash?: string
  provenResolvedStateKey?: string
  provenResolvedLockfilePath?: string
  provenResolvedLockfileHash?: string
}): { ok: boolean; reason: string } {
  const proof = input.proof
  if (!proof) return { ok: false, reason: 'proof missing' }
  if (proof.project !== input.project || proof.branch !== input.branch) return { ok: false, reason: 'project/branch identity mismatch' }
  if (proof.assignmentHash !== materializationAssignmentHash(input.actions)) return { ok: false, reason: 'assignment hash mismatch' }
  if (input.provenEnvelopeKey && proof.provenEnvelopeKey !== input.provenEnvelopeKey) return { ok: false, reason: 'proven envelope mismatch' }
  if (input.provenAssignmentKey && proof.provenAssignmentKey !== input.provenAssignmentKey) return { ok: false, reason: 'proven assignment mismatch' }
  if (input.resolverInputKey && proof.resolverInputKey !== input.resolverInputKey) return { ok: false, reason: 'resolver proof identity mismatch' }
  if (input.fixedResolverInputsKey && proof.fixedResolverInputsKey !== input.fixedResolverInputsKey) return { ok: false, reason: 'fixed source proof identity mismatch' }
  if (input.sourceSnapshotKey && proof.sourceSnapshotKey !== input.sourceSnapshotKey) return { ok: false, reason: 'source snapshot identity mismatch' }
  if (input.projectProofKey && proof.projectProofKey !== input.projectProofKey) return { ok: false, reason: 'project proof identity mismatch' }
  if (input.provenObservedResolvedHash && proof.provenObservedResolvedHash !== input.provenObservedResolvedHash) return { ok: false, reason: 'proven observed resolved hash mismatch' }
  if (input.provenResolvedStateKey && proof.provenResolvedStateKey !== input.provenResolvedStateKey) return { ok: false, reason: 'proven resolved state mismatch' }
  if (input.provenResolvedLockfilePath && proof.provenResolvedLockfilePath !== input.provenResolvedLockfilePath) return { ok: false, reason: 'proven resolved lockfile path mismatch' }
  if (input.provenResolvedLockfileHash && proof.provenResolvedLockfileHash !== input.provenResolvedLockfileHash) return { ok: false, reason: 'proven resolved lockfile hash mismatch' }
  if (input.provenExactDirectAssignment && canonicalJson(proof.provenExactDirectAssignment ?? {}) !== canonicalJson(input.provenExactDirectAssignment)) return { ok: false, reason: 'proven exact direct assignment mismatch' }
  if (input.provenRemovals && canonicalJson(proof.provenRemovals ?? []) !== canonicalJson([...input.provenRemovals].map(String).sort())) return { ok: false, reason: 'proven removals mismatch' }
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
  if (!proof.provenResolvedStateKey || !proof.provenResolvedLockfilePath || !proof.provenResolvedLockfileHash) {
    return { ok: false, reason: 'proven resolved-state contract missing' }
  }
  const provenLockfile = resolve(input.projectPath, proof.provenResolvedLockfilePath)
  if (!existsSync(provenLockfile)) return { ok: false, reason: 'proven resolved lockfile missing' }
  if (sha256(readFileSync(provenLockfile)) !== proof.provenResolvedLockfileHash) {
    return { ok: false, reason: 'proven resolved lockfile digest changed' }
  }

  let observed: Record<string, string>
  try {
    observed = observedResolvedVersions(input.projectPath, proof.actions)
  } catch (error) {
    return { ok: false, reason: error instanceof Error ? error.message : String(error) }
  }
  if (sha256(canonicalJson(observed)) !== proof.observedResolvedHash) {
    return { ok: false, reason: 'observed resolved tuple changed' }
  }

  const provenAssignment = input.provenExactDirectAssignment ?? proof.provenExactDirectAssignment
  const provenRemovals = input.provenRemovals ?? proof.provenRemovals
  const provenHash = input.provenObservedResolvedHash ?? proof.provenObservedResolvedHash
  const hasProvenObservedContract = Boolean(provenAssignment || provenRemovals || provenHash)
  if (hasProvenObservedContract) {
    if (!provenAssignment || !provenRemovals || !provenHash) {
      return { ok: false, reason: 'proven observed resolved contract incomplete' }
    }
    let currentProvenHash = ''
    try {
      currentProvenHash = observedResolvedDirectAssignmentHash(
        input.projectPath,
        provenAssignment,
        provenRemovals,
      )
    } catch (error) {
      return { ok: false, reason: error instanceof Error ? error.message : String(error) }
    }
    if (currentProvenHash !== provenHash) {
      return {
        ok: false,
        reason: `OBSERVED_PROVEN_ASSIGNMENT_DRIFT: baseline=${provenHash} current=${currentProvenHash}`,
      }
    }
  }
  return { ok: true, reason: 'materialization proof matches ProofEnvelope and current dependency state' }
}
