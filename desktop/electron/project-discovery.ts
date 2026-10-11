// Single-rule project-manifest discovery for the Desktop pre-flight.
//
// Canonical ownership: Python `project_topology.discover_project_package_directories`
// remains the authoritative topology used by an actual migration. This module is
// the ONE place the Desktop keeps the matching walk so the renderer can offer a
// bounded scan report (candidates + depth truncation + lockfile grouping) without
// duplicating rules. `desktop/scripts/check-project-discovery.mjs` pins this
// implementation against the Python walk on real fixture trees, and the bounded
// async driver must reproduce the exact candidate set of the sync walk.
//
// The scan is BOUNDED: a shared budget of visited directories and a wall-clock
// deadline apply to the whole traversal (not per-branch), and the asynchronous
// driver yields to the event loop periodically so a large tree never blocks the
// Electron main thread for a single uncuttable slice. Any budget stop reports
// `truncated=true` with an explicit `budgetHit` reason, so the renderer can
// offer the scan-extension action and never pretend a partial view is complete.
import { statSync, readFileSync, readdirSync, type Dirent } from 'node:fs'
import { dirname, join, relative, resolve, sep } from 'node:path'

export const PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH = 5
export const PROJECT_MANIFEST_DISCOVERY_DEEP_SCAN_DEPTH = 8
export const PROJECT_MANIFEST_DISCOVERY_IGNORED_DIRS: ReadonlySet<string> = new Set([
  '.git', 'node_modules', '.dependency-roadmap', '.dependency-update-history',
  '.next', 'dist', 'build', 'coverage', '.turbo', '.cache',
])

/** Default shared caps for the async bounded walk. */
export const PROJECT_MANIFEST_DISCOVERY_DEFAULT_MAX_ENTRIES = 20_000
export const PROJECT_MANIFEST_DISCOVERY_DEFAULT_TIME_BUDGET_MS = 2_000
/** Yield to the event loop after this many visited directories. */
export const PROJECT_MANIFEST_DISCOVERY_YIELD_EVERY = 256

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

export type DiscoveryBudgetHit = 'depth' | 'entries' | 'time'

export type DiscoveryReport = {
  root: string
  maxDepth: number
  candidates: DiscoveryCandidate[]
  /** The scan stopped early — because of the depth limit, the entry budget or the time budget. */
  truncated: boolean
  /** Manifests that exist strictly below maxDepth and were therefore not picked up. */
  deeperManifests: number
  deepestSeen: number
  /** Why the scan stopped early, when it did: depth limit, shared entry budget or wall-clock deadline. */
  budgetHit?: DiscoveryBudgetHit
}

export type BoundedWalkOptions = {
  /** Shared cap on the number of visited directories (whole traversal). */
  maxEntries?: number
  /** Shared wall-clock deadline, in milliseconds from the walk start. */
  timeBudgetMs?: number
}

const LOCKFILE_PRIORITY: ReadonlyArray<{ kind: DiscoveryLockfileKind; name: string }> = [
  { kind: 'npm-shrinkwrap', name: 'npm-shrinkwrap.json' },
  { kind: 'npm', name: 'package-lock.json' },
  { kind: 'yarn', name: 'yarn.lock' },
  { kind: 'pnpm', name: 'pnpm-lock.yaml' },
]

const MANIFEST_FILE = 'package.json'

function fileExists(path: string): boolean {
  try {
    return statSync(path).isFile()
  } catch {
    return false
  }
}

/** Expand `{a,b}` brace alternatives in a workspace pattern, like Python's `expand_braces`. */
function expandBraceAlternatives(pattern: string): string[] {
  const opening = pattern.indexOf('{')
  if (opening === -1) return [pattern]
  let depth = 0
  let closing = -1
  let comma = -1
  for (let index = opening; index < pattern.length; index += 1) {
    const char = pattern[index]
    if (char === '{') depth += 1
    else if (char === '}') {
      depth -= 1
      if (depth === 0) { closing = index; break }
    } else if (char === ',' && depth === 1 && comma === -1) comma = index
  }
  if (closing === -1 || comma === -1) return [pattern]
  const head = pattern.slice(0, opening)
  const tail = pattern.slice(closing + 1)
  const inside = pattern.slice(opening + 1, closing)
  const alternatives = inside.split(',')
  const expanded: string[] = []
  for (const alternative of alternatives) {
    for (const inner of expandBraceAlternatives(head + alternative + tail)) expanded.push(inner)
  }
  return expanded
}

function translateGlobSegment(segment: string): string {
  let out = ''
  for (let index = 0; index < segment.length; index += 1) {
    const char = segment[index]
    if (char === '*') out += '[^/]*'
    else if (char === '?') out += '[^/]'
    else if (char === '[') {
      let close = index + 1
      if (segment[close] === '!' || segment[close] === '^') close += 1
      if (segment[close] === ']') close += 1
      while (close < segment.length && segment[close] !== ']') close += 1
      if (close < segment.length) {
        const content = segment.slice(index + 1, close)
        out += `[${content.replace(/\\/g, '\\\\')}]`
        index = close
      } else {
        out += '\\['
      }
    } else if (char === '\\') {
      index += 1
      out += index < segment.length ? segment[index].replace(/[.^$|(){}[\]\\+?]/g, '\\$&') : ''
    } else {
      out += char.replace(/[.^$|(){}[\]\\+?]/g, '\\$&')
    }
  }
  return out
}

function globstarRegex(segments: string[]): string {
  const runs: Array<string[] | null> = []
  let current: string[] = []
  for (const segment of segments) {
    if (segment === '**') {
      if (current.length) { runs.push(current); current = [] }
      if (!runs.length || runs[runs.length - 1] !== null) runs.push(null)
    } else {
      current.push(segment)
    }
  }
  if (current.length) runs.push(current)
  if (runs.every((run) => run === null)) return '^.*$'
  let out = ''
  if (runs[0] === null) {
    out = '(?:[^/]+/)*'
    runs.shift()
  }
  runs.forEach((run, index) => {
    if (run === null) out += index < runs.length - 1 ? '(?:/[^/]+)*/' : '(?:/[^/]+)*'
    else out += run.map(translateGlobSegment).join('/')
  })
  return `^${out}$`
}

/**
 * Whether a workspace pattern matches a POSIX relative path, mirroring Python
 * `workspace_glob.match_workspace_pattern`: `*` stays within one segment, a
 * whole-segment `**` matches zero or more intermediate folders, `?` matches one
 * character, `[...]` is a class, and `{a,b}` expands to alternatives.
 */
export function workspacePatternMatches(pattern: string, relPath: string): boolean {
  const segments = relPath.replace(/^\/+|\/+$/g, '').split('/')
  if (!pattern.includes('{')) {
    return matchSegments(pattern.split('/'), segments)
  }
  for (const expanded of expandBraceAlternatives(pattern)) {
    const parts = expanded.split('/')
    if (parts[0] === '' && parts.length > 1) parts.shift()
    if (matchSegments(parts, segments)) return true
  }
  return false
}

function matchSegments(patternSegments: string[], relSegments: string[]): boolean {
  if (!patternSegments.length) return relSegments.length === 0
  if (patternSegments.includes('**')) {
    return new RegExp(globstarRegex(patternSegments)).test(relSegments.join('/'))
  }
  if (patternSegments.length !== relSegments.length) return false
  for (let index = 0; index < patternSegments.length; index += 1) {
    const regex = new RegExp(`^${translateGlobSegment(patternSegments[index])}$`)
    if (!regex.test(relSegments[index])) return false
  }
  return true
}

/** The raw `workspaces` of a manifest, resolved to concrete patterns. */
function workspacePatternsOf(managerDir: string): string[] | undefined {
  const manifestPath = join(managerDir, MANIFEST_FILE)
  if (!fileExists(manifestPath)) return undefined
  try {
    const manifest = JSON.parse(readFileSync(manifestPath, 'utf8')) as { workspaces?: unknown }
    const value = manifest.workspaces
    if (typeof value === 'string' && value.trim()) return [value.trim()]
    if (Array.isArray(value)) {
      const patterns = value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0).map((item) => item.trim())
      return patterns.length ? patterns : undefined
    }
    // Yarn classic nests { packages: [...] } — read the flat list only; the
    // rest is handled by Python for an actual migration.
    if (value && typeof value === 'object' && Array.isArray((value as { packages?: unknown }).packages)) {
      const patterns = ((value as { packages: unknown[] }).packages).filter((item): item is string => typeof item === 'string' && item.trim().length > 0).map((item) => item.trim())
      return patterns.length ? patterns : undefined
    }
    return undefined
  } catch {
    return undefined
  }
}

/**
 * Whether an ancestor manager directory OWNS ``dir`` through its workspace
 * patterns — the same rule Python `project_topology._workspace_owns` applies, so
 * the Desktop never attributes a FOREIGN ancestor lockfile to a subproject.
 * A ``!``-prefixed exclusion pattern overrides an inclusion.
 */
function managerOwnsDir(managerDir: string, dir: string): boolean {
  const patterns = workspacePatternsOf(managerDir)
  if (!patterns || !patterns.length) return false
  const relPath = relative(resolve(managerDir), resolve(dir)).split(sep).join('/')
  if (relPath.startsWith('..') || relPath.split('/').includes('..')) return false
  if (relPath === '.') return true
  let owned = false
  for (const pattern of patterns) {
    const negate = pattern.startsWith('!')
    const body = negate ? pattern.slice(1) : pattern
    if (!body.trim()) continue
    if (workspacePatternMatches(body.trim(), relPath)) {
      if (negate) return false
      owned = true
    }
  }
  return owned
}

/**
 * The canonical lockfile governing ``startDir`` within ``boundaryRoot``.
 *
 * Mirrors Python `_canonical_for_project`: the directory's OWN lockfile always
 * wins; an ancestor lockfile is adopted ONLY when that ancestor is a workspace
 * manager whose patterns own ``startDir``. A foreign ancestor lockfile (e.g.
 * some unrelated monorepo root above a bare subfolder) is never attributed to
 * the subproject — Python rejects exactly that attribution.
 */
export function canonicalLockfileForDir(startDir: string, boundaryRoot: string): DiscoveryLockfile {
  const resolvedBoundary = resolve(boundaryRoot)
  let current = resolve(startDir)
  let guard = 0
  while (guard++ < 32) {
    const isSelf = current === resolve(startDir)
    if (!isSelf && !managerOwnsDir(current, startDir)) {
      const parent = dirname(current)
      if (current === resolvedBoundary || parent === current) break
      current = parent
      continue
    }
    for (const entry of LOCKFILE_PRIORITY) {
      const candidatePath = join(current, entry.name)
      if (fileExists(candidatePath)) return { kind: entry.kind, name: entry.name, path: candidatePath }
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
    const manifest = JSON.parse(readFileSync(join(directory, MANIFEST_FILE), 'utf8')) as { name?: unknown }
    return typeof manifest.name === 'string' && manifest.name.trim() ? manifest.name.trim() : undefined
  } catch {
    return undefined
  }
}

type TraversalStep =
  | { kind: 'manifest'; directory: string; depth: number; packageName?: string; lockfile?: DiscoveryLockfile }
  | { kind: 'children'; directory: string; depth: number; children: Array<{ directory: string; depth: number }> }

/**
 * The single, rule-owning traversal. `process` is called for every visited
 * directory in pre-order; the caller drives it synchronously or in bounded
 * asynchronous chunks, and both drivers produce the identical candidate set.
 *
 * The walk descends through every non-manifest directory even BEYOND the depth
 * limit: a manifest below the limit must still surface as a scan-extension
 * hint. Depth interpretation (candidate vs hint) belongs to the driver, so the
 * sync and async drivers stay identical.
 */
function* walkSteps(root: string): Generator<TraversalStep, void, void> {
  const resolvedRoot = resolve(root)
  const stack: Array<{ directory: string; depth: number }> = [{ directory: resolvedRoot, depth: 0 }]
  while (stack.length) {
    const { directory, depth } = stack.pop() as { directory: string; depth: number }
    const isRoot = directory === resolvedRoot
    const hasManifest = !isRoot && fileExists(join(directory, MANIFEST_FILE))
    if (hasManifest) {
      const packageName = packageNameFor(directory)
      const manifestStep: Extract<TraversalStep, { kind: 'manifest' }> = {
        kind: 'manifest',
        directory,
        depth,
        ...(packageName ? { packageName } : {}),
        lockfile: canonicalLockfileForDir(directory, resolvedRoot),
      }
      yield manifestStep
      continue
    }
    let entries: Dirent[] = []
    try {
      entries = readdirSync(directory, { withFileTypes: true })
    } catch {
      continue
    }
    const children: Array<{ directory: string; depth: number }> = []
    for (const entry of entries) {
      if (!entry.isDirectory() || entry.isSymbolicLink()) continue
      if (PROJECT_MANIFEST_DISCOVERY_IGNORED_DIRS.has(entry.name)) continue
      const child = { directory: join(directory, entry.name), depth: depth + 1 }
      children.push(child)
      stack.push(child)
    }
    yield { kind: 'children', directory, depth, children }
  }
}

function makeCandidate(step: Extract<TraversalStep, { kind: 'manifest' }>, resolvedRoot: string): DiscoveryCandidate {
  const candidate: DiscoveryCandidate = {
    path: resolve(step.directory),
    relative: relative(resolvedRoot, step.directory).split(sep).join('/'),
    depth: step.depth,
    ...(step.packageName ? { packageName: step.packageName } : {}),
    lockfile: step.lockfile ?? { kind: 'none', name: null },
  }
  return candidate
}

export function discoverProjectDirectories(
  root: string,
  maxDepth: number = PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH,
): DiscoveryReport {
  return runBoundedSync(root, maxDepth, {})
}

function runBoundedSync(root: string, maxDepth: number, options: BoundedWalkOptions): DiscoveryReport {
  const resolvedRoot = resolve(root)
  const limit = Math.max(0, Math.min(64, Number.isFinite(maxDepth) ? maxDepth : PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH))
  const found: DiscoveryCandidate[] = []
  const ctx = { truncated: false, deeperManifests: 0, deepestSeen: 0 }
  let budgetHit: DiscoveryBudgetHit | undefined
  let visited = 0
  const maxEntries = options.maxEntries !== undefined ? Math.max(1, Math.floor(options.maxEntries)) : Number.POSITIVE_INFINITY
  const deadline = options.timeBudgetMs !== undefined && options.timeBudgetMs > 0 ? Date.now() + options.timeBudgetMs : undefined

  for (const step of walkSteps(resolvedRoot)) {
    visited += 1
    if (visited > maxEntries) {
      ctx.truncated = true
      budgetHit = 'entries'
      break
    }
    if (deadline !== undefined && Date.now() >= deadline) {
      ctx.truncated = true
      budgetHit = 'time'
      break
    }
    if (step.depth > limit) {
      ctx.truncated = true
      if (budgetHit === undefined) budgetHit = 'depth'
      if (step.kind === 'manifest') ctx.deeperManifests += 1
      continue
    }
    if (step.kind === 'manifest') {
      found.push(makeCandidate(step, resolvedRoot))
    } else {
      for (const child of step.children) ctx.deepestSeen = Math.max(ctx.deepestSeen, child.depth)
    }
  }
  found.sort((left, right) => left.relative.localeCompare(right.relative) || left.path.localeCompare(right.path))
  return {
    root: resolvedRoot,
    maxDepth: limit,
    candidates: found,
    truncated: ctx.truncated,
    deeperManifests: ctx.deeperManifests,
    deepestSeen: ctx.deepestSeen,
    ...(budgetHit ? { budgetHit } : {}),
  }
}

/**
 * The bounded ASYNCHRONOUS scan for the renderer: shared entry/time budgets
 * over the whole traversal, yielding to the event loop every few directories so
 * a large tree never stalls the Electron main thread. The candidate set is
 * identical to `discoverProjectDirectories` when the budgets are not hit.
 */
export async function discoverProjectDirectoriesBounded(
  root: string,
  maxDepth: number = PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH,
  options: BoundedWalkOptions = {},
): Promise<DiscoveryReport> {
  const resolvedRoot = resolve(root)
  const limit = Math.max(0, Math.min(64, Number.isFinite(maxDepth) ? maxDepth : PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH))
  const found: DiscoveryCandidate[] = []
  const ctx = { truncated: false, deeperManifests: 0, deepestSeen: 0 }
  let budgetHit: DiscoveryBudgetHit | undefined
  let visited = 0
  const maxEntries = options.maxEntries !== undefined ? Math.max(1, Math.floor(options.maxEntries)) : Number.POSITIVE_INFINITY
  const deadline = options.timeBudgetMs !== undefined && options.timeBudgetMs > 0 ? Date.now() + options.timeBudgetMs : undefined
  const yieldEvery = Math.max(64, Math.min(4096, PROJECT_MANIFEST_DISCOVERY_YIELD_EVERY))

  for (const step of walkSteps(resolvedRoot)) {
    visited += 1
    if (visited > maxEntries) {
      ctx.truncated = true
      budgetHit = 'entries'
      break
    }
    if (deadline !== undefined && Date.now() >= deadline) {
      ctx.truncated = true
      budgetHit = 'time'
      break
    }
    if (step.depth > limit) {
      ctx.truncated = true
      if (budgetHit === undefined) budgetHit = 'depth'
      if (step.kind === 'manifest') ctx.deeperManifests += 1
      continue
    }
    if (step.kind === 'manifest') {
      found.push(makeCandidate(step, resolvedRoot))
    } else {
      for (const child of step.children) ctx.deepestSeen = Math.max(ctx.deepestSeen, child.depth)
    }
    if (visited % yieldEvery === 0) await new Promise<void>((resolveYield) => setImmediate(resolveYield))
  }
  found.sort((left, right) => left.relative.localeCompare(right.relative) || left.path.localeCompare(right.path))
  return {
    root: resolvedRoot,
    maxDepth: limit,
    candidates: found,
    truncated: ctx.truncated,
    deeperManifests: ctx.deeperManifests,
    deepestSeen: ctx.deepestSeen,
    ...(budgetHit ? { budgetHit } : {}),
  }
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
  if (fileExists(join(selected, MANIFEST_FILE))) return { status: 'explicit', path: selected }
  const report = discoverProjectDirectories(selected, maxDepth)
  if (report.candidates.length === 0) return { status: 'missing', path: selected, report }
  if (report.candidates.length === 1) return { status: 'single', path: report.candidates[0].path, report }
  return { status: 'ambiguous', path: selected, report }
}

/** Build a selection from an already-computed (possibly bounded) report. */
export function selectionFromReport(inputPath: string, report: DiscoveryReport): PackageSelection {
  const selected = resolve(inputPath)
  if (fileExists(join(selected, MANIFEST_FILE))) return { status: 'explicit', path: selected }
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
  budgetHit?: DiscoveryBudgetHit
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
    ...(report.budgetHit ? { budgetHit: report.budgetHit } : {}),
  }
}
