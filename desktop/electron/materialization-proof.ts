import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'

export type MaterializationAction = {
  package: string
  target: string
  action: string
}

export type DependencyMaterializationProof = {
  schemaVersion: 1
  project: string
  branch: string
  assignmentHash: string
  actions: MaterializationAction[]
  dependencySections: Record<string, Record<string, string>>
  dependencySectionsHash: string
  dependencyControlFields: Record<string, unknown>
  dependencyStateHash: string
  lockfiles: Record<string, string>
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
    .map((item) => ({ package: String(item.package), target: String(item.target), action: String(item.action || 'update') }))
    .sort((a, b) => a.package.localeCompare(b.package) || a.target.localeCompare(b.target) || a.action.localeCompare(b.action))
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

export function materializationProofPath(workspacePath: string, project: string, branch: string): string {
  const safe = (value: string) => value.replace(/[^a-zA-Z0-9._-]+/g, '-')
  return join(workspacePath, '.dependency-roadmap', 'state', 'materialization-proofs', safe(project), `${safe(branch)}.json`)
}

export function createMaterializationProof(input: {
  projectPath: string
  project: string
  branch: string
  actions: readonly MaterializationAction[]
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
  return {
    schemaVersion: 1,
    project: input.project,
    branch: input.branch,
    assignmentHash: materializationAssignmentHash(actions),
    actions,
    dependencySections,
    dependencySectionsHash: sha256(canonicalJson(dependencySections)),
    dependencyControlFields,
    dependencyStateHash: sha256(canonicalJson({ dependencySections, dependencyControlFields })),
    lockfiles: lockfileHashes(input.projectPath),
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
    return value?.schemaVersion === 1 ? value : undefined
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
}): { ok: boolean; reason: string } {
  const proof = input.proof
  if (!proof) return { ok: false, reason: 'proof missing' }
  if (proof.project !== input.project || proof.branch !== input.branch) return { ok: false, reason: 'project/branch identity mismatch' }
  if (proof.assignmentHash !== materializationAssignmentHash(input.actions)) return { ok: false, reason: 'assignment hash mismatch' }
  if (proof.packageManager !== input.packageManager) return { ok: false, reason: 'package-manager mismatch' }
  if (input.packageManagerVersion && proof.packageManagerVersion !== input.packageManagerVersion) return { ok: false, reason: 'package-manager version mismatch' }
  if (input.nodeVersion && proof.nodeVersion !== input.nodeVersion) return { ok: false, reason: 'Node version mismatch' }
  let packageJson: unknown
  try { packageJson = JSON.parse(readFileSync(join(input.projectPath, 'package.json'), 'utf8')) as unknown } catch { return { ok: false, reason: 'package.json unreadable' } }
  if (dependencySectionsHash(packageJson) !== proof.dependencySectionsHash) return { ok: false, reason: 'dependency sections changed' }
  if (dependencyStateHash(packageJson) !== proof.dependencyStateHash) return { ok: false, reason: 'dependency control fields changed' }
  const currentLockfiles = lockfileHashes(input.projectPath)
  if (canonicalJson(currentLockfiles) !== canonicalJson(proof.lockfiles)) return { ok: false, reason: 'lockfile digest changed' }
  return { ok: true, reason: 'materialization proof matches current dependency state' }
}
