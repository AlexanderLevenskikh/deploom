// Single-rule project-manifest discovery for the Desktop pre-flight.
//
// Canonical ownership: Python `project_topology.discover_project_package_directories`
// remains the authoritative topology used by an actual migration. This module is
// the ONE place the Desktop keeps the matching walk so the renderer can offer a
// bounded scan report (candidates + depth truncation + lockfile grouping) without
// duplicating rules. `desktop/scripts/check-project-discovery.mjs` pins this
// implementation against the Python walk on real fixture trees.
import { existsSync, readFileSync, readdirSync, type Dirent } from 'node:fs'
import { dirname, join, relative, resolve, sep } from 'node:path'

export const PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH = 5
export const PROJECT_MANIFEST_DISCOVERY_DEEP_SCAN_DEPTH = 8
export const PROJECT_MANIFEST_DISCOVERY_IGNORED_DIRS: ReadonlySet<string> = new Set([
  '.git', 'node_modules', '.dependency-roadmap', '.dependency-update-history',
  '.next', 'dist', 'build', 'coverage', '.turbo', '.cache',
])

export type DiscoveryLockfileKind = 'npm-shrinkwrap' | 'npm' | 'yarn' | 'pnpm'

export type DiscoveryLockfile =
  | { kind: DiscoveryLockfileKind; name: string; path?: string }
  | { kind: 'none'; name: null; path?: undefined }

export type DiscoveryCandidate = {
  path: string
  /** POSIX path of the package root relative to the scan root; '' for the root itself. */
  relative: string
  depth: number
  packageName?: string
  lockfile: DiscoveryLockfile
}

export type DiscoveryReport = {
  root: string
  maxDepth: number
  candidates: DiscoveryCandidate[]
  /** The scan stopped early because it exceeded maxDepth. */
  truncated: boolean
  /** Manifests that exist strictly below maxDepth and were therefore not picked up. */
  deeperManifests: number
  deepestSeen: number
}

const LOCKFILE_PRIORITY: ReadonlyArray<{ kind: DiscoveryLockfileKind; name: string }> = [
  { kind: 'npm-shrinkwrap', name: 'npm-shrinkwrap.json' },
  { kind: 'npm', name: 'package-lock.json' },
  { kind: 'yarn', name: 'yarn.lock' },
  { kind: 'pnpm', name: 'pnpm-lock.yaml' },
]

export function canonicalLockfileForDir(startDir: string, boundaryRoot: string): DiscoveryLockfile {
  const resolvedBoundary = resolve(boundaryRoot)
  let current = resolve(startDir)
  // Guard against walking above the filesystem root.
  let guard = 0
  while (guard++ < 32) {
    for (const entry of LOCKFILE_PRIORITY) {
      const candidatePath = join(current, entry.name)
      if (existsSync(candidatePath)) return { kind: entry.kind, name: entry.name, path: candidatePath }
    }
    if (current === resolvedBoundary) break
    const parent = dirname(current)
    if (parent === current) break
    current = parent
  }
  return { kind: 'none', name: null }
}

function packageNameFor(directory: string): string | undefined {
  try {
    const manifest = JSON.parse(readFileSync(join(directory, 'package.json'), 'utf8')) as { name?: unknown }
    return typeof manifest.name === 'string' && manifest.name.trim() ? manifest.name.trim() : undefined
  } catch {
    return undefined
  }
}

export function discoverProjectDirectories(
  root: string,
  maxDepth: number = PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH,
): DiscoveryReport {
  const resolvedRoot = resolve(root)
  const limit = Math.max(0, Math.min(64, Number.isFinite(maxDepth) ? maxDepth : PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH))
  const found: DiscoveryCandidate[] = []
  let truncated = false
  let deeperManifests = 0
  let deepestSeen = 0

  const walk = (directory: string, depth: number): void => {
    const hasManifest = directory !== resolvedRoot && existsSync(join(directory, 'package.json'))
    if (depth > limit) {
      truncated = true
      if (hasManifest) deeperManifests += 1
      return
    }
    if (hasManifest) {
      const lockfile = canonicalLockfileForDir(directory, resolvedRoot)
      const packageName = packageNameFor(directory)
      found.push({
        path: resolve(directory),
        relative: relative(resolvedRoot, directory).split(sep).join('/'),
        depth,
        ...(packageName ? { packageName } : {}),
        lockfile,
      })
      return
    }
    let entries: Dirent[]
    try {
      entries = readdirSync(directory, { withFileTypes: true })
    } catch {
      return
    }
    for (const entry of entries) {
      if (!entry.isDirectory() || entry.isSymbolicLink()) continue
      if (PROJECT_MANIFEST_DISCOVERY_IGNORED_DIRS.has(entry.name)) continue
      deepestSeen = Math.max(deepestSeen, depth + 1)
      walk(join(directory, entry.name), depth + 1)
    }
  }

  walk(resolvedRoot, 0)
  found.sort((left, right) => left.relative.localeCompare(right.relative) || left.path.localeCompare(right.path))
  return { root: resolvedRoot, maxDepth: limit, candidates: found, truncated, deeperManifests, deepestSeen }
}

export type PackageSelection =
  | { status: 'explicit'; path: string }
  | { status: 'single'; path: string; report: DiscoveryReport }
  | { status: 'missing'; path: string; report: DiscoveryReport }
  | { status: 'ambiguous'; path: string; report: DiscoveryReport }

export function resolveProjectPackageSelection(
  inputPath: string,
  maxDepth: number = PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH,
): PackageSelection {
  const selected = resolve(inputPath)
  if (existsSync(join(selected, 'package.json'))) return { status: 'explicit', path: selected }
  const report = discoverProjectDirectories(selected, maxDepth)
  if (report.candidates.length === 0) return { status: 'missing', path: selected, report }
  if (report.candidates.length === 1) return { status: 'single', path: report.candidates[0].path, report }
  return { status: 'ambiguous', path: selected, report }
}

export function deepScanSuggestion(report: DiscoveryReport): boolean {
  return report.truncated && report.deeperManifests > 0
}

export type SerializedDiscoveryCandidate = {
  path: string
  relative: string
  depth: number
  packageName?: string
  lockfileKind: string
  lockfileName: string | null
  lockfilePath?: string
}

export type SerializedDiscoveryReport = {
  root: string
  maxDepth: number
  candidates: SerializedDiscoveryCandidate[]
  truncated: boolean
  deeperManifests: number
  deepestSeen: number
}

export function serializeDiscoveryReport(report: DiscoveryReport): SerializedDiscoveryReport {
  return {
    root: report.root,
    maxDepth: report.maxDepth,
    candidates: report.candidates.map((candidate) => ({
      path: candidate.path,
      relative: candidate.relative,
      depth: candidate.depth,
      ...(candidate.packageName ? { packageName: candidate.packageName } : {}),
      lockfileKind: candidate.lockfile.kind,
      lockfileName: candidate.lockfile.name,
      ...(candidate.lockfile.path ? { lockfilePath: candidate.lockfile.path } : {}),
    })),
    truncated: report.truncated,
    deeperManifests: report.deeperManifests,
    deepestSeen: report.deepestSeen,
  }
}
